using System;
using SDRSharp.SttPlugin.Dsp;

namespace SDRSharp.SttPlugin.Tests;

public class DecimatorTests
{
    private const double FromRate = 48_000;
    private const double ToRate   = 16_000;

    private static float[] Sine(double freqHz, double sampleRate, int count)
    {
        var s = new float[count];
        for (int i = 0; i < count; i++)
            s[i] = (float)Math.Sin(2 * Math.PI * freqHz * i / sampleRate);
        return s;
    }

    private static double RmsDb(float[] samples, int skip)
    {
        double sum = 0;
        int n = 0;
        for (int i = skip; i < samples.Length - skip; i++)
        {
            sum += samples[i] * (double)samples[i];
            n++;
        }
        double rms = Math.Sqrt(sum / Math.Max(1, n));
        return 20 * Math.Log10(Math.Max(rms, 1e-12));
    }

    [Fact]
    public void Resample_RejectsAliasingTone_By60dBOrMore()
    {
        // 12 kHz at 48 kHz -> 16 kHz output: a naive linear-interpolation decimator lets
        // this fold back almost undamped. The windowed-sinc filter should reject it hard.
        const int count = 96_000; // 2 s at 48 kHz
        var passbandTone = Sine(2_000, FromRate, count);   // well within the ~7.2 kHz cutoff
        var aliasingTone = Sine(12_000, FromRate, count);  // above target Nyquist (8 kHz)

        var passbandOut = Decimator.Resample(passbandTone, FromRate, ToRate);
        var aliasingOut = Decimator.Resample(aliasingTone, FromRate, ToRate);

        int skip = 200; // drop filter-edge transients
        double passbandDb = RmsDb(passbandOut, skip);
        double aliasingDb = RmsDb(aliasingOut, skip);

        double attenuationDb = passbandDb - aliasingDb;
        Assert.True(attenuationDb > 60,
            $"expected >60 dB attenuation, got {attenuationDb:F1} dB " +
            $"(passband {passbandDb:F1} dB, aliasing {aliasingDb:F1} dB)");
    }

    [Fact]
    public void Resample_PreservesDcLevel()
    {
        var dc = new float[4000];
        Array.Fill(dc, 0.5f);

        var result = Decimator.Resample(dc, FromRate, ToRate);

        for (int i = 50; i < result.Length - 50; i++)
            Assert.InRange(result[i], 0.45f, 0.55f);
    }

    [Fact]
    public void Resample_SameRate_ReturnsInputUnchanged()
    {
        var input = Sine(1000, 48_000, 100);
        var result = Decimator.Resample(input, 48_000, 48_000);
        Assert.Same(input, result);
    }

    [Fact]
    public void Resample_EmptyInput_ReturnsEmpty()
    {
        var result = Decimator.Resample(Array.Empty<float>(), FromRate, ToRate);
        Assert.Empty(result);
    }

    [Fact]
    public void Resample_NonIntegerRatio_FallsBackWithoutThrowing()
    {
        // 48 kHz -> 22.05 kHz is not an integer decimation factor.
        var input = Sine(1000, 48_000, 4800);
        var result = Decimator.Resample(input, 48_000, 22_050);

        Assert.NotEmpty(result);
        int expectedLen = (int)(input.Length * 22_050.0 / 48_000.0);
        Assert.InRange(result.Length, expectedLen - 2, expectedLen + 2);
    }

    [Fact]
    public void DesignLowPass_ProducesUnityDcGain()
    {
        var taps = Decimator.DesignLowPass(7200, 48_000, 63);
        double sum = 0;
        foreach (var t in taps) sum += t;
        Assert.InRange(sum, 0.999, 1.001);
    }
}
