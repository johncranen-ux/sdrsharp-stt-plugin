using System;
using SDRSharp.SttPlugin.Dsp;

namespace SDRSharp.SttPlugin.Tests;

public class BiquadFiltersTests
{
    [Fact]
    public void DcBlocker_RemovesConstantOffset()
    {
        var blocker = new DcBlocker();
        var input = new float[5000];
        Array.Fill(input, 0.5f);

        var output = blocker.Process(input);

        // Skip the settling transient; the tail should have converged near zero.
        for (int i = 4000; i < output.Length; i++)
            Assert.InRange(output[i], -0.01f, 0.01f);
    }

    [Fact]
    public void DcBlocker_PassesAcSignalThrough()
    {
        var blocker = new DcBlocker();
        const int n = 2000;
        var input = new float[n];
        for (int i = 0; i < n; i++)
            input[i] = (float)Math.Sin(2 * Math.PI * 1000 * i / 48_000.0);

        var output = blocker.Process(input);

        double inEnergy = 0, outEnergy = 0;
        for (int i = 500; i < n; i++)
        {
            inEnergy += input[i] * (double)input[i];
            outEnergy += output[i] * (double)output[i];
        }
        // A 1 kHz tone should pass through with little loss.
        Assert.True(outEnergy > inEnergy * 0.8, $"in={inEnergy:F1} out={outEnergy:F1}");
    }

    [Fact]
    public void HighPassBiquad_AttenuatesSubCutoffMoreThanPassband()
    {
        const double sampleRate = 48_000;
        const int count = 48_000; // 1 s

        float[] Tone(double freq)
        {
            var s = new float[count];
            for (int i = 0; i < count; i++) s[i] = (float)Math.Sin(2 * Math.PI * freq * i / sampleRate);
            return s;
        }

        double RmsDb(float[] s, int skip)
        {
            double sum = 0;
            int n = 0;
            for (int i = skip; i < s.Length; i++) { sum += s[i] * (double)s[i]; n++; }
            return 20 * Math.Log10(Math.Max(Math.Sqrt(sum / Math.Max(1, n)), 1e-12));
        }

        var below = new HighPassBiquad(sampleRate, 150).Process(Tone(20));    // well below cutoff
        var above = new HighPassBiquad(sampleRate, 150).Process(Tone(1000)); // well above cutoff

        int skip = 2000; // let the filter settle
        double belowDb = RmsDb(below, skip);
        double aboveDb = RmsDb(above, skip);

        Assert.True(aboveDb - belowDb > 20,
            $"expected passband to be >20 dB louder than sub-cutoff, got below={belowDb:F1} above={aboveDb:F1}");
    }

    [Fact]
    public void HighPassBiquad_Reset_ClearsState()
    {
        var filter = new HighPassBiquad(48_000, 150);
        for (int i = 0; i < 100; i++) filter.Process(1.0f);
        filter.Reset();

        // Immediately after reset, a zero input must produce exactly zero output.
        Assert.Equal(0f, filter.Process(0f));
    }
}
