using System;
using System.IO;
using System.Text;

namespace SDRSharp.SttPlugin
{
    internal static class WavBuilder
    {
        private static readonly byte[] RiffTag = Encoding.ASCII.GetBytes("RIFF");
        private static readonly byte[] WaveTag = Encoding.ASCII.GetBytes("WAVE");
        private static readonly byte[] FmtTag  = Encoding.ASCII.GetBytes("fmt ");
        private static readonly byte[] DataTag = Encoding.ASCII.GetBytes("data");

        public static float[] Resample(float[] samples, double fromRate, double toRate)
        {
            if (samples == null || samples.Length == 0) return Array.Empty<float>();
            if (Math.Abs(fromRate - toRate) < 1.0) return samples;

            int outLen = (int)(samples.Length * toRate / fromRate);
            if (outLen <= 0) return Array.Empty<float>();

            var result = new float[outLen];
            double ratio = (double)(samples.Length - 1) / Math.Max(1, outLen - 1);
            for (int i = 0; i < outLen; i++)
            {
                double pos  = i * ratio;
                int    idx  = (int)pos;
                double frac = pos - idx;
                float  a    = samples[idx];
                float  b    = (idx + 1 < samples.Length) ? samples[idx + 1] : a;
                result[i]   = (float)(a + frac * (b - a));
            }
            return result;
        }

        public static byte[] Build(float[] samples, double sampleRate)
        {
            if (samples == null || samples.Length == 0)
                return Array.Empty<byte>();

            const int bitsPerSample = 16;
            const int numChannels   = 1;
            const int audioFormat   = 1;  // PCM

            int sampleRateInt = (int)sampleRate;
            int byteRate      = sampleRateInt * numChannels * (bitsPerSample / 8);
            int blockAlign    = numChannels * (bitsPerSample / 8);
            int dataSize      = samples.Length * (bitsPerSample / 8);

            using var ms     = new MemoryStream(44 + dataSize);
            using var writer = new BinaryWriter(ms, Encoding.ASCII, leaveOpen: false);

            writer.Write(RiffTag);
            writer.Write(36 + dataSize);
            writer.Write(WaveTag);

            writer.Write(FmtTag);
            writer.Write(16);
            writer.Write((short)audioFormat);
            writer.Write((short)numChannels);
            writer.Write(sampleRateInt);
            writer.Write(byteRate);
            writer.Write((short)blockAlign);
            writer.Write((short)bitsPerSample);

            writer.Write(DataTag);
            writer.Write(dataSize);

            foreach (var sample in samples)
            {
                var pcm = (short)Math.Clamp(sample * 32_767f, -32_768f, 32_767f);
                writer.Write(pcm);
            }

            return ms.ToArray();
        }
    }
}
