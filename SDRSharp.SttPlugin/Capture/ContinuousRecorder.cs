using System;
using System.Collections.Concurrent;
using System.IO;
using System.Threading;
using System.Threading.Tasks;

namespace SDRSharp.SttPlugin.Capture
{
    // Streams the undecimated post-filter audio straight from the real-time callback to a
    // WAV file on disk, so VAD/DSP changes can be replayed offline against unsegmented audio
    // instead of requiring a live session for every experiment.
    public sealed class ContinuousRecorder : IDisposable
    {
        private readonly string _pluginDir;
        private readonly BlockingCollection<float[]> _queue =
            new BlockingCollection<float[]>(new ConcurrentQueue<float[]>(), 500);

        private FileStream? _stream;
        private BinaryWriter? _writer;
        private long _sampleCount;
        private double _sampleRate;
        private Task? _writerTask;
        private CancellationTokenSource? _cts;
        private volatile bool _running;

        public ContinuousRecorder(string pluginDir)
        {
            _pluginDir = pluginDir;
        }

        public bool IsRunning => _running;

        public void Start(double sampleRate)
        {
            if (_running) return;

            var dir = Path.Combine(_pluginDir, "captures", "raw-continuous");
            Directory.CreateDirectory(dir);
            var path = Path.Combine(dir, $"{DateTime.Now:yyyy-MM-dd_HHmmss}.wav");

            _sampleRate = sampleRate;
            _sampleCount = 0;
            _stream = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.Read);
            _writer = new BinaryWriter(_stream);
            WriteHeaderPlaceholder(_writer, sampleRate);

            _cts = new CancellationTokenSource();
            _running = true;
            _writerTask = Task.Run(() => WriteLoop(_cts.Token));
        }

        public void Stop()
        {
            if (!_running) return;
            _running = false;

            try { _cts?.Cancel(); } catch { }
            // Explicit double literal: an int argument here binds to .NET 9's newer
            // TimeSpan.FromSeconds(long) overload at compile time, which doesn't exist on
            // the .NET 8 runtime SDR# actually hosts plugins on ("Method not found").
            try { _writerTask?.Wait(TimeSpan.FromSeconds(5.0)); } catch { }

            try
            {
                if (_writer != null && _stream != null)
                {
                    PatchHeader(_writer, _stream, _sampleCount);
                    _writer.Flush();
                }
            }
            catch { }
            finally
            {
                _writer?.Dispose();
                _stream?.Dispose();
                _writer = null;
                _stream = null;
                _cts?.Dispose();
                _cts = null;
            }
        }

        // Called from the real-time audio thread. Must not block or allocate beyond the copy.
        public void Enqueue(float[] samples)
        {
            if (!_running) return;
            if (!_queue.TryAdd(samples))
                _queue.TryTake(out _); // drop oldest rather than block the audio thread
        }

        private void WriteLoop(CancellationToken token)
        {
            try
            {
                while (!token.IsCancellationRequested)
                {
                    float[] samples;
                    try { samples = _queue.Take(token); }
                    catch (OperationCanceledException) { break; }

                    if (_writer == null) continue;
                    foreach (var sample in samples)
                    {
                        var pcm = (short)Math.Clamp(sample * 32_767f, -32_768f, 32_767f);
                        _writer.Write(pcm);
                    }
                    _sampleCount += samples.Length;
                }

                // Drain whatever is left so the tail of the session isn't lost.
                while (_queue.TryTake(out var samples))
                {
                    if (_writer == null) continue;
                    foreach (var sample in samples)
                    {
                        var pcm = (short)Math.Clamp(sample * 32_767f, -32_768f, 32_767f);
                        _writer.Write(pcm);
                    }
                    _sampleCount += samples.Length;
                }
            }
            catch { }
        }

        private static void WriteHeaderPlaceholder(BinaryWriter w, double sampleRate)
        {
            const int bitsPerSample = 16, numChannels = 1, audioFormat = 1;
            int sampleRateInt = (int)sampleRate;
            int byteRate      = sampleRateInt * numChannels * (bitsPerSample / 8);
            int blockAlign    = numChannels * (bitsPerSample / 8);

            w.Write(System.Text.Encoding.ASCII.GetBytes("RIFF"));
            w.Write(0); // patched later
            w.Write(System.Text.Encoding.ASCII.GetBytes("WAVE"));
            w.Write(System.Text.Encoding.ASCII.GetBytes("fmt "));
            w.Write(16);
            w.Write((short)audioFormat);
            w.Write((short)numChannels);
            w.Write(sampleRateInt);
            w.Write(byteRate);
            w.Write((short)blockAlign);
            w.Write((short)bitsPerSample);
            w.Write(System.Text.Encoding.ASCII.GetBytes("data"));
            w.Write(0); // patched later
        }

        private static void PatchHeader(BinaryWriter w, FileStream s, long sampleCount)
        {
            int dataSize = (int)(sampleCount * 2);
            w.Flush();
            s.Seek(4, SeekOrigin.Begin);
            w.Write(36 + dataSize);
            s.Seek(40, SeekOrigin.Begin);
            w.Write(dataSize);
        }

        public void Dispose()
        {
            Stop();
            _queue.Dispose();
        }
    }
}
