using System;
using System.Globalization;
using System.IO;
using System.Text;
using SDRSharp.SttPlugin;

namespace SDRSharp.SttPlugin.Capture
{
    // One VAD-segmented transmission, as sent to the server plus the raw pre-resample audio.
    public sealed class ChunkCaptureInfo
    {
        public required float[] RawSamples    { get; init; }
        public required double  RawSampleRate { get; init; }
        public required float[] SentSamples   { get; init; }
        public required double  SentSampleRate{ get; init; }
        public string?  Channel      { get; init; }
        public string?  DetectorType { get; init; }
        public float    Rms          { get; init; }
        public float    ActiveRatio  { get; init; }
        public float    NormalizeGain{ get; init; } = 1f;
        public string   ReturnedText { get; init; } = "";
    }

    // Writes each sent chunk (raw + resampled WAV) plus a JSONL sidecar line, so
    // server/bench.py can replay VAD/DSP decisions offline against real captured audio.
    public sealed class ChunkRecorder
    {
        private readonly string _baseDir;
        private readonly object _lock = new object();
        private string? _dayDir;
        private int _index;

        public bool Enabled { get; set; }

        public ChunkRecorder(string pluginDir)
        {
            _baseDir = Path.Combine(pluginDir, "captures");
        }

        public void Record(ChunkCaptureInfo info)
        {
            if (!Enabled) return;

            try
            {
                lock (_lock)
                {
                    EnsureDayDir();
                    var rawPath  = Path.Combine(_dayDir!, $"{_index:D4}_raw.wav");
                    var sentPath = Path.Combine(_dayDir!, $"{_index:D4}_sent.wav");

                    File.WriteAllBytes(rawPath,  WavBuilder.Build(info.RawSamples,  info.RawSampleRate));
                    File.WriteAllBytes(sentPath, WavBuilder.Build(info.SentSamples, info.SentSampleRate));

                    AppendIndexLine(_index, info);
                    _index++;
                }
            }
            catch
            {
                // Capture is diagnostic-only; never let a disk error affect transcription.
            }
        }

        private void EnsureDayDir()
        {
            if (_dayDir != null) return;

            var day = DateTime.Now.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
            _dayDir = Path.Combine(_baseDir, day);
            Directory.CreateDirectory(_dayDir);

            // Resume numbering after a restart instead of overwriting today's captures.
            _index = 0;
            foreach (var file in Directory.EnumerateFiles(_dayDir, "*_sent.wav"))
            {
                var name = Path.GetFileNameWithoutExtension(file);
                var numPart = name.Substring(0, name.IndexOf('_'));
                if (int.TryParse(numPart, out var n) && n >= _index)
                    _index = n + 1;
            }
        }

        // Hand-rolled rather than System.Text.Json: see the comment on
        // WhisperClient.ExtractText — that assembly's version is tied to the target
        // framework and fails to load on the .NET 8 runtime SDR# actually hosts plugins on.
        private void AppendIndexLine(int index, ChunkCaptureInfo info)
        {
            var timestamp   = DateTime.Now.ToString("o", CultureInfo.InvariantCulture);
            var durationSec = Math.Round(info.SentSamples.Length / Math.Max(1.0, info.SentSampleRate), 3);

            string channelJson      = info.Channel      is null ? "null" : $"\"{JsonEscape(info.Channel)}\"";
            string detectorTypeJson = info.DetectorType is null ? "null" : $"\"{JsonEscape(info.DetectorType)}\"";

            var line =
                "{" +
                $"\"index\":{index}," +
                $"\"timestamp\":\"{JsonEscape(timestamp)}\"," +
                $"\"channel\":{channelJson}," +
                $"\"detectorType\":{detectorTypeJson}," +
                $"\"durationSec\":{durationSec.ToString(CultureInfo.InvariantCulture)}," +
                $"\"rms\":{info.Rms.ToString(CultureInfo.InvariantCulture)}," +
                $"\"activeRatio\":{info.ActiveRatio.ToString(CultureInfo.InvariantCulture)}," +
                $"\"normalizeGain\":{info.NormalizeGain.ToString(CultureInfo.InvariantCulture)}," +
                $"\"text\":\"{JsonEscape(info.ReturnedText)}\"" +
                "}" + Environment.NewLine;

            File.AppendAllText(Path.Combine(_dayDir!, "index.jsonl"), line, Encoding.UTF8);
        }

        private static string JsonEscape(string? s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            var sb = new StringBuilder(s.Length);
            foreach (var c in s)
            {
                switch (c)
                {
                    case '"':  sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else sb.Append(c);
                        break;
                }
            }
            return sb.ToString();
        }
    }
}
