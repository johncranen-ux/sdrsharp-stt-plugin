using System;

namespace SDRSharp.SttPlugin.Dsp
{
    // Anti-aliased sample-rate conversion: windowed-sinc FIR low-pass followed by
    // decimation (or filter-then-linear-interpolate for non-integer ratios). Replaces bare
    // linear interpolation, which lets everything above the target Nyquist fold back into
    // the kept band — the dominant source of noise on NFM/hiss-heavy VHF audio.
    public static class Decimator
    {
        private const int DefaultTaps = 63;

        public static float[] Resample(float[] samples, double fromRate, double toRate)
        {
            if (samples == null || samples.Length == 0) return Array.Empty<float>();
            if (Math.Abs(fromRate - toRate) < 1.0) return samples;

            // Cut off a bit below the destination Nyquist so the FIR's transition band
            // finishes rolling off before aliasing back in.
            double cutoffHz = Math.Min(fromRate, toRate) / 2.0 * 0.90;
            var taps = DesignLowPass(cutoffHz, fromRate, DefaultTaps);

            double ratio = fromRate / toRate;
            bool isIntegerDecimation = ratio >= 1.0 && Math.Abs(ratio - Math.Round(ratio)) < 1e-6;

            if (isIntegerDecimation)
                return PolyphaseDecimate(samples, taps, (int)Math.Round(ratio));

            var filtered = Convolve(samples, taps);
            return LinearResample(filtered, fromRate, toRate);
        }

        // Windowed-sinc low-pass, 4-term Blackman-Harris window (~92 dB nominal sidelobe
        // level). cutoffHz / sampleRate must be in (0, 0.5).
        internal static float[] DesignLowPass(double cutoffHz, double sampleRate, int numTaps)
        {
            if (numTaps < 3) throw new ArgumentOutOfRangeException(nameof(numTaps));

            double fc = cutoffHz / sampleRate;
            int m = numTaps - 1;
            var h = new double[numTaps];

            for (int n = 0; n < numTaps; n++)
            {
                double k = n - m / 2.0;
                double sinc = Math.Abs(k) < 1e-9 ? 2 * fc : Math.Sin(2 * Math.PI * fc * k) / (Math.PI * k);
                double w = 0.35875
                         - 0.48829 * Math.Cos(2 * Math.PI * n / m)
                         + 0.14128 * Math.Cos(4 * Math.PI * n / m)
                         - 0.01168 * Math.Cos(6 * Math.PI * n / m);
                h[n] = sinc * w;
            }

            double sum = 0;
            foreach (var v in h) sum += v;

            var result = new float[numTaps];
            for (int i = 0; i < numTaps; i++)
                result[i] = (float)(h[i] / sum); // unity DC gain
            return result;
        }

        // y[n] = sum_k h[k] * x[nD - k]; only the kept output samples are computed, so this
        // never materializes the full filtered signal at the source rate.
        internal static float[] PolyphaseDecimate(float[] x, float[] h, int decimation)
        {
            int center = h.Length / 2;
            int outLen = x.Length / decimation;
            var y = new float[outLen];

            for (int n = 0; n < outLen; n++)
            {
                double acc = 0;
                int baseIdx = n * decimation + center;
                for (int k = 0; k < h.Length; k++)
                {
                    int xi = baseIdx - k;
                    if ((uint)xi < (uint)x.Length) acc += h[k] * x[xi];
                }
                y[n] = (float)acc;
            }
            return y;
        }

        internal static float[] Convolve(float[] x, float[] h)
        {
            int center = h.Length / 2;
            var y = new float[x.Length];
            for (int n = 0; n < x.Length; n++)
            {
                double acc = 0;
                for (int k = 0; k < h.Length; k++)
                {
                    int xi = n + center - k;
                    if ((uint)xi < (uint)x.Length) acc += h[k] * x[xi];
                }
                y[n] = (float)acc;
            }
            return y;
        }

        internal static float[] LinearResample(float[] samples, double fromRate, double toRate)
        {
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
    }
}
