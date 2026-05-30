using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using SDRSharp.Radio;

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

        private int _droppedFrames;

        private readonly WhisperClient _whisperClient;
        private readonly SDRSharp.Common.ISharpControl? _control;
        private readonly CancellationTokenSource _cts = new CancellationTokenSource();

        private bool _enabled;

        // Interlocked double trick: volatile is not valid for double in C#.
        private long _sampleRateBits = BitConverter.DoubleToInt64Bits(48_000);
        private volatile bool _debugSaveNext;
        private volatile int  _vadLevel  = 10;   // 0 = disabled; 1-100 maps to RMS 0.001-0.100
        private volatile int  _silenceMs = 600;  // ms of trailing silence before sending

        private int          _frameCount;
        private int          _enabledFrameCount;
        private volatile string _consumerState = "not started";

        public bool   DebugSaveNext      { set => _debugSaveNext = value; }
        public int    FrameCount         => _frameCount;
        public int    EnabledFrameCount  => _enabledFrameCount;
        public int    QueueCount         => _queue.Count;
        public int    DroppedFrames      => _droppedFrames;
        public string ConsumerState      => _consumerState;
        public float  LastRms            { get; private set; }

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

        public AudioProcessor(WhisperClient whisperClient, SDRSharp.Common.ISharpControl? control = null)
        {
            _whisperClient = whisperClient;
            _control       = control;
            _ = Task.Run(ConsumeAsync);
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
                samples[i] = buffer[i * 2];  // left channel only → mono

            if (!_queue.TryAdd(samples))
            {
                _queue.TryTake(out _);
                _queue.TryAdd(samples);
                _droppedFrames++;
            }
        }

        private void ConsumeAsync()
        {
            const int MAX_SPEECH_SEC = 30;

            _consumerState = "running";
            var speechBuf     = new List<float>((int)(48_000 * MAX_SPEECH_SEC));
            var pendingFrames = new List<float>(4096);
            var token         = _cts.Token;
            bool inSpeech     = false;
            int  silentFrames = 0;

            try
            {
                while (!token.IsCancellationRequested)
                {
                    _consumerState = inSpeech
                        ? $"speech ({speechBuf.Count / (int)SampleRate}s)"
                        : "silent";

                    float[] raw;
                    try { raw = _queue.Take(token); }
                    catch (OperationCanceledException) { break; }

                    int vadLevel      = _vadLevel;
                    int frameSize     = Math.Max(1, (int)(SampleRate * 0.02));
                    int silenceFrames = Math.Max(1, _silenceMs / 20);

                    // VAD disabled: fixed 5-second window
                    if (vadLevel == 0)
                    {
                        speechBuf.AddRange(raw);
                        int target = (int)(SampleRate * 5);
                        while (speechBuf.Count >= target)
                        {
                            SendChunk(speechBuf.GetRange(0, target).ToArray());
                            speechBuf.RemoveRange(0, target);
                        }
                        continue;
                    }

                    float threshold = vadLevel / 1000f;
                    pendingFrames.AddRange(raw);

                    while (pendingFrames.Count >= frameSize)
                    {
                        float rms   = Rms(pendingFrames, 0, frameSize);
                        LastRms     = rms;
                        bool active = rms >= threshold;

                        var frame = pendingFrames.GetRange(0, frameSize);
                        pendingFrames.RemoveRange(0, frameSize);

                        if (!inSpeech)
                        {
                            if (active)
                            {
                                inSpeech     = true;
                                silentFrames = 0;
                                speechBuf.AddRange(frame);
                            }
                        }
                        else
                        {
                            speechBuf.AddRange(frame);

                            if (active)
                                silentFrames = 0;
                            else
                                silentFrames++;

                            bool endOfSpeech = silentFrames >= silenceFrames;
                            bool tooLong     = speechBuf.Count >= (int)(SampleRate * MAX_SPEECH_SEC);

                            if (endOfSpeech || tooLong)
                            {
                                SendChunk(speechBuf.ToArray());
                                speechBuf.Clear();
                                if (endOfSpeech)
                                {
                                    inSpeech     = false;
                                    silentFrames = 0;
                                }
                                else
                                {
                                    silentFrames = 0;
                                }
                            }
                        }
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

        private const double WhisperRate = 16_000.0;

        private void SendChunk(float[] chunk)
        {
            var resampled = WavBuilder.Resample(chunk, SampleRate, WhisperRate);

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

            _consumerState = "sending";
            try
            {
                var task = _whisperClient.SendAsync(resampled, WhisperRate, channel);
                if (!task.Wait(TimeSpan.FromSeconds(30.0)))
                {
                    _consumerState = "send timeout";
                    return;
                }
                if (task.IsFaulted)
                {
                    _consumerState = $"send error: {task.Exception?.InnerException?.Message}";
                    return;
                }
            }
            catch (Exception ex)
            {
                _consumerState = $"send error: {ex.Message}";
                return;
            }
            _consumerState = "sent";
        }

        private static float Rms(float[] samples, int offset, int length)
        {
            double sum = 0;
            for (int i = offset; i < offset + length; i++) sum += samples[i] * samples[i];
            return (float)Math.Sqrt(sum / length);
        }

        private static float Rms(List<float> samples, int offset, int length)
        {
            double sum = 0;
            for (int i = offset; i < offset + length; i++) sum += samples[i] * samples[i];
            return (float)Math.Sqrt(sum / length);
        }

        public void Dispose()
        {
            _enabled = false;
            _cts.Cancel();
            _queue.CompleteAdding();
            _cts.Dispose();
            _queue.Dispose();
        }
    }
}
