using System;

namespace SDRSharp.SttPlugin.Dsp
{
    // One-pole DC blocker: y[n] = x[n] - x[n-1] + r*y[n-1]. Removes DC offset / very
    // low-frequency drift without touching the rest of the spectrum.
    public sealed class DcBlocker
    {
        private readonly float _r;
        private float _prevX;
        private float _prevY;

        public DcBlocker(float r = 0.995f) => _r = r;

        public void Reset() => (_prevX, _prevY) = (0f, 0f);

        public float Process(float x)
        {
            float y = x - _prevX + _r * _prevY;
            _prevX = x;
            _prevY = y;
            return y;
        }

        public float[] Process(float[] samples)
        {
            var result = new float[samples.Length];
            for (int i = 0; i < samples.Length; i++) result[i] = Process(samples[i]);
            return result;
        }
    }

    // Second-order Butterworth high-pass (RBJ Audio EQ Cookbook formulas), Q = 0.7071.
    // Removes NFM rumble and CTCSS tones (67-250 Hz) sitting below the speech band.
    public sealed class HighPassBiquad
    {
        private readonly float _b0, _b1, _b2, _a1, _a2;
        private float _x1, _x2, _y1, _y2;

        public HighPassBiquad(double sampleRate, double cutoffHz, double q = 0.7071)
        {
            double w0    = 2 * Math.PI * cutoffHz / sampleRate;
            double alpha = Math.Sin(w0) / (2 * q);
            double cosW0 = Math.Cos(w0);

            double b0 = (1 + cosW0) / 2;
            double b1 = -(1 + cosW0);
            double b2 = (1 + cosW0) / 2;
            double a0 = 1 + alpha;
            double a1 = -2 * cosW0;
            double a2 = 1 - alpha;

            _b0 = (float)(b0 / a0);
            _b1 = (float)(b1 / a0);
            _b2 = (float)(b2 / a0);
            _a1 = (float)(a1 / a0);
            _a2 = (float)(a2 / a0);
        }

        public void Reset() => (_x1, _x2, _y1, _y2) = (0f, 0f, 0f, 0f);

        public float Process(float x)
        {
            float y = _b0 * x + _b1 * _x1 + _b2 * _x2 - _a1 * _y1 - _a2 * _y2;
            _x2 = _x1; _x1 = x;
            _y2 = _y1; _y1 = y;
            return y;
        }

        public float[] Process(float[] samples)
        {
            var result = new float[samples.Length];
            for (int i = 0; i < samples.Length; i++) result[i] = Process(samples[i]);
            return result;
        }
    }
}
