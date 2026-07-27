using System;

namespace SDRSharp.SttPlugin.Dsp
{
    // Per-chunk peak normalization with a soft limiter for any residual overshoot.
    public static class Normalizer
    {
        public readonly struct Result
        {
            public readonly float[] Samples;
            public readonly float   Gain; // linear gain actually applied

            public Result(float[] samples, float gain) { Samples = samples; Gain = gain; }
        }

        public static Result Normalize(float[] samples, float targetPeakDb = -1.0f)
        {
            if (samples == null || samples.Length == 0) return new Result(Array.Empty<float>(), 1f);

            float peak = 0f;
            foreach (var s in samples)
            {
                var a = MathF.Abs(s);
                if (a > peak) peak = a;
            }

            // Don't amplify a near-silent/noise-only chunk up to full scale.
            if (peak < 1e-6f) return new Result((float[])samples.Clone(), 1f);

            float targetPeak = MathF.Pow(10f, targetPeakDb / 20f);
            float gain = targetPeak / peak;

            var result = new float[samples.Length];
            for (int i = 0; i < samples.Length; i++)
                result[i] = SoftClip(samples[i] * gain);

            return new Result(result, gain);
        }

        private static float SoftClip(float x)
        {
            const float threshold = 0.98f;
            if (x > threshold)  return threshold + (1 - threshold) * MathF.Tanh((x - threshold) / (1 - threshold));
            if (x < -threshold) return -threshold + (1 - threshold) * MathF.Tanh((x + threshold) / (1 - threshold));
            return x;
        }
    }
}
