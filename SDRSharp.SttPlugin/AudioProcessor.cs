using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using SDRSharp.Radio;
using SDRSharp.SttPlugin.Capture;
using SDRSharp.SttPlugin.Dsp;

namespace SDRSharp.SttPlugin
{
    public sealed class AudioProcessor : IRealProcessor, IStreamProcessor, IBaseProcessor, IDisposable
    {
        private static readonly string _pluginDir =
            System.IO.Path.GetDirectoryName(
                System.Reflection.Assembly.GetExecutingAssembly().Location)
            ?? System.IO.Path.GetTempPath();

        // Bounded to 1500 frames (~30 seconds at 48 kHz, 20 ms frames).
        private readonly BlockingCollection<float[]> _queue =
            new BlockingCollection<float[]>(new ConcurrentQueue<float[]>(), 1500);

        // Decouples the network send (which can take seconds) from the VAD consumer, so a
        // slow/stalled server can no longer stall frame processing and cause the raw-audio
        // queue above to drop samples. Small capacity: chunks arrive at most every few
        // seconds, so a backlog here means the server has fallen far behind.
        private readonly BlockingCollection<PendingChunk> _sendQueue =
            new BlockingCollection<PendingChunk>(new ConcurrentQueue<PendingChunk>(), 10);

        private readonly record struct PendingChunk(float[] Samples, float ActiveRatio, float AvgRms);

        private int _droppedFrames;
        private int _droppedChunks;

        private readonly WhisperClient _whisperClient;
        private readonly SDRSharp.Common.ISharpControl? _control;
        private readonly CancellationTokenSource _cts = new CancellationTokenSource();
        private readonly ChunkRecorder _chunkRecorder;
        private readonly ContinuousRecorder _continuousRecorder;

        private bool _enabled;

        // Interlocked double trick: volatile is not valid for double in C#.
        private long _sampleRateBits = BitConverter.DoubleToInt64Bits(48_000);
        private volatile bool _debugSaveNext;
        // 0 = disabled (fixed 5s chunks); 1-100 maps to the VAD's absolute RMS floor
        // (0.001-0.100), below which the adaptive noise-floor gate never opens regardless
        // of ambient level.
        private volatile int  _vadLevel  = 10;
        private volatile int  _silenceMs = 600;  // ms of trailing silence before closing a segment

        private readonly VadConfig _vadConfig = new VadConfig();
        private readonly VoiceActivityDetector _vad;

        private int          _frameCount;
        private int          _enabledFrameCount;
        private volatile string _consumerState = "not started";

        public bool   DebugSaveNext      { set => _debugSaveNext = value; }
        public int    FrameCount         => _frameCount;
        public int    EnabledFrameCount  => _enabledFrameCount;
        public int    QueueCount         => _queue.Count;
        public int    DroppedFrames      => _droppedFrames;
        public int    SendQueueCount     => _sendQueue.Count;
        public int    DroppedChunks      => _droppedChunks;
        public string ConsumerState      => _consumerState;
        public float  LastRms            { get; private set; }
        public float  NoiseFloor         => _vad.NoiseFloor;

        public bool Enabled
        {
            get => _enabled;
            set => _enabled = value;
        }

        public double SampleRate
        {
            get => BitConverter.Int64BitsToDouble(Interlocked.Read(ref _sampleRateBits));
            set => Interlocked.Exchange(ref _sampleRateBits, BitConverter.DoubleToInt64Bits(value));
        }

        public int VadLevel
        {
            get => _vadLevel;
            set => _vadLevel = Math.Max(0, Math.Min(100, value));
        }

        public int SilenceMs
        {
            get => _silenceMs;
            set => _silenceMs = Math.Max(100, Math.Min(3000, value));
        }

        // Diagnostic capture, off by default: writes every sent chunk (raw + resampled WAV
        // + JSONL sidecar) so server/bench.py can replay VAD/DSP decisions offline.
        public bool CaptureChunks
        {
            get => _chunkRecorder.Enabled;
            set => _chunkRecorder.Enabled = value;
        }

        // Continuous undecimated raw stream, for replaying VAD changes against real audio.
        public bool CaptureContinuous
        {
            get => _continuousRecorder.IsRunning;
            set
            {
                if (value) _continuousRecorder.Start(SampleRate);
                else       _continuousRecorder.Stop();
            }
        }

        public AudioProcessor(WhisperClient whisperClient, SDRSharp.Common.ISharpControl? control = null)
        {
            _whisperClient       = whisperClient;
            _control             = control;
            _chunkRecorder       = new ChunkRecorder(_pluginDir);
            _continuousRecorder  = new ContinuousRecorder(_pluginDir);
            _vadConfig.SampleRate = SampleRate;
            _vadConfig.SilenceMs  = _silenceMs;
            _vadConfig.AbsoluteRmsFloor = _vadLevel / 1000f;
            _vad = new VoiceActivityDetector(_vadConfig);
            _ = Task.Run(ConsumeAsync);
            _ = Task.Run(SendLoopAsync);
        }

        // Called by SDR# on the real-time audio thread for every post-filter buffer.
        // FilteredAudioOutput delivers stereo-interleaved samples (L, R, L, R…).
        public unsafe void Process(float* buffer, int length)
        {
            Interlocked.Increment(ref _frameCount);
            if (!_enabled || length <= 0) return;
            Interlocked.Increment(ref _enabledFrameCount);

            var samples = new float[length / 2];
            for (int i = 0; i < samples.Length; i++)
                samples[i] = (buffer[i * 2] + buffer[i * 2 + 1]) * 0.5f;  // mix L+R → mono

            _continuousRecorder.Enqueue(samples);

            if (!_queue.TryAdd(samples))
            {
                _queue.TryTake(out _);
                _queue.TryAdd(samples);
                _droppedFrames++;
            }
        }

        private void ConsumeAsync()
        {
            _consumerState = "running";

            // Fallback path only used while VAD is disabled (VadLevel == 0): fixed 5-second
            // windows with no speech detection at all.
            var fixedWindowBuf = new List<float>((int)(48_000 * 5));
            var pendingFrames  = new List<float>(4096);
            var token          = _cts.Token;
            int lastVadLevel   = _vadLevel;

            try
            {
                while (!token.IsCancellationRequested)
                {
                    _consumerState = _vad.InSpeech ? "speech" : "silent";

                    float[] raw;
                    try { raw = _queue.Take(token); }
                    catch (OperationCanceledException) { break; }

                    int vadLevel = _vadLevel;

                    // Toggling VAD on/off mid-session leaves stale partial state in whichever
                    // path was active; discard it rather than mixing modes.
                    if ((vadLevel == 0) != (lastVadLevel == 0))
                    {
                        _vad.Reset();
                        fixedWindowBuf.Clear();
                        pendingFrames.Clear();
                    }
                    lastVadLevel = vadLevel;

                    if (vadLevel == 0)
                    {
                        fixedWindowBuf.AddRange(raw);
                        int target = (int)(SampleRate * 5);
                        while (fixedWindowBuf.Count >= target)
                        {
                            var fixedChunk = fixedWindowBuf.GetRange(0, target).ToArray();
                            fixedWindowBuf.RemoveRange(0, target);
                            EnqueueForSend(new PendingChunk(fixedChunk, 1f, Rms(fixedChunk)));
                        }
                        continue;
                    }

                    _vadConfig.SampleRate       = SampleRate;
                    _vadConfig.SilenceMs        = _silenceMs;
                    _vadConfig.AbsoluteRmsFloor = vadLevel / 1000f;

                    bool? squelchOpen = ReadSquelchOpen();

                    pendingFrames.AddRange(raw);
                    int frameSize = _vad.FrameSize;

                    while (pendingFrames.Count >= frameSize)
                    {
                        var frame = pendingFrames.GetRange(0, frameSize).ToArray();
                        pendingFrames.RemoveRange(0, frameSize);

                        var chunk = _vad.ProcessFrame(frame, squelchOpen);
                        LastRms = _vad.LastRms;

                        if (chunk != null)
                            EnqueueForSend(new PendingChunk(chunk.Samples, chunk.ActiveRatio, chunk.AvgRms));
                    }
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception ex)
            {
                _consumerState = $"crashed: {ex.GetType().Name}: {ex.Message}";
                try
                {
                    System.IO.File.WriteAllText(
                        System.IO.Path.Combine(_pluginDir, "consumer_crash.log"),
                        $"{DateTime.Now:o}\n{ex}\n");
                }
                catch { }
            }
        }

        private void EnqueueForSend(PendingChunk pending)
        {
            if (!_sendQueue.TryAdd(pending))
            {
                _sendQueue.TryTake(out _);
                _sendQueue.TryAdd(pending);
                _droppedChunks++;
            }
        }

        private void SendLoopAsync()
        {
            var token = _cts.Token;
            try
            {
                while (!token.IsCancellationRequested)
                {
                    PendingChunk pending;
                    try { pending = _sendQueue.Take(token); }
                    catch (OperationCanceledException) { break; }

                    SendChunk(pending.Samples, pending.ActiveRatio, pending.AvgRms);
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception ex)
            {
                _consumerState = $"send loop crashed: {ex.GetType().Name}: {ex.Message}";
                try
                {
                    System.IO.File.WriteAllText(
                        System.IO.Path.Combine(_pluginDir, "send_loop_crash.log"),
                        $"{DateTime.Now:o}\n{ex}\n");
                }
                catch { }
            }
        }

        // Null when SDR#'s squelch is unavailable or the user hasn't enabled it, in which
        // case the VAD falls back to its adaptive RMS gate.
        private bool? ReadSquelchOpen()
        {
            try
            {
                if (_control == null || !_control.SquelchEnabled) return null;
                return _control.IsSquelchOpen;
            }
            catch
            {
                return null;
            }
        }

        private const double WhisperRate = 16_000.0;
        private const double HighPassCutoffHz = 150.0;

        private void SendChunk(float[] chunk, float activeRatio, float chunkRms)
        {
            // Fresh filter state per chunk: chunks are independent VAD segments, not a
            // continuous stream, and each carries leading padding to absorb the settle time.
            var dcBlocker  = new DcBlocker();
            var highPass   = new HighPassBiquad(SampleRate, HighPassCutoffHz);
            var conditioned = new float[chunk.Length];
            for (int i = 0; i < chunk.Length; i++)
                conditioned[i] = highPass.Process(dcBlocker.Process(chunk[i]));

            var decimated = Decimator.Resample(conditioned, SampleRate, WhisperRate);
            var normalized = Normalizer.Normalize(decimated);
            var resampled = normalized.Samples;

            if (_debugSaveNext)
            {
                _debugSaveNext = false;
                try
                {
                    var wav = WavBuilder.Build(resampled, WhisperRate);
                    System.IO.File.WriteAllBytes(
                        System.IO.Path.Combine(_pluginDir, "debug_chunk.wav"), wav);
                }
                catch { }
            }

            string? channel = null;
            try
            {
                long? freqNullable = _control?.Frequency;
                if (freqNullable.HasValue)
                    channel = (freqNullable.Value / 1_000_000m).ToString("F3");
            }
            catch { }

            string? detectorType = null;
            try { detectorType = _control?.DetectorType.ToString(); }
            catch { }

            _consumerState = "sending";
            string returnedText = "";
            try
            {
                // Runs on the dedicated send-loop thread, not the VAD consumer, so blocking
                // here no longer risks stalling frame processing. WhisperClient's own 60s
                // response timeout is the single source of truth (previously this method
                // imposed a shorter 30s cap that raced with it, orphaning the socket work).
                returnedText   = _whisperClient.SendAsync(resampled, WhisperRate, channel)
                                                .GetAwaiter().GetResult();
                _consumerState = "sent";
            }
            catch (Exception ex)
            {
                _consumerState = $"send error: {ex.Message}";
            }

            if (_chunkRecorder.Enabled)
            {
                _chunkRecorder.Record(new ChunkCaptureInfo
                {
                    RawSamples     = chunk,
                    RawSampleRate  = SampleRate,
                    SentSamples    = resampled,
                    SentSampleRate = WhisperRate,
                    Channel        = channel,
                    DetectorType   = detectorType,
                    Rms            = chunkRms,
                    ActiveRatio    = activeRatio,
                    NormalizeGain  = normalized.Gain,
                    ReturnedText   = returnedText,
                });
            }
        }

        private static float Rms(float[] samples)
        {
            if (samples.Length == 0) return 0f;
            double sum = 0;
            foreach (var s in samples) sum += s * (double)s;
            return (float)Math.Sqrt(sum / samples.Length);
        }

        public void Dispose()
        {
            _enabled = false;
            _cts.Cancel();
            _queue.CompleteAdding();
            _sendQueue.CompleteAdding();
            _cts.Dispose();
            _queue.Dispose();
            _sendQueue.Dispose();
            _continuousRecorder.Dispose();
        }
    }
}
