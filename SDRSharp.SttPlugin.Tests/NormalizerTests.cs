using System;
using SDRSharp.SttPlugin.Dsp;

namespace SDRSharp.SttPlugin.Tests;

public class NormalizerTests
{
    [Fact]
    public void Normalize_ScalesPeakToTarget()
    {
        var input = new float[] { 0.0f, 0.1f, -0.05f, 0.02f };
        var result = Normalizer.Normalize(input, targetPeakDb: -1.0f);

        float expectedTargetPeak = MathF.Pow(10f, -1.0f / 20f); // ~0.891
        float actualPeak = 0f;
        foreach (var s in result.Samples) actualPeak = MathF.Max(actualPeak, MathF.Abs(s));

        Assert.InRange(actualPeak, expectedTargetPeak - 0.001f, expectedTargetPeak + 0.001f);
        Assert.True(result.Gain > 1f, "a quiet 0.1-peak signal should be amplified");
    }

    [Fact]
    public void Normalize_NearSilence_DoesNotAmplify()
    {
        var input = new float[] { 0f, 1e-8f, -1e-8f, 0f };
        var result = Normalizer.Normalize(input);

        Assert.Equal(1f, result.Gain);
        Assert.Equal(input, result.Samples);
    }

    [Fact]
    public void Normalize_EmptyInput_ReturnsEmpty()
    {
        var result = Normalizer.Normalize(Array.Empty<float>());
        Assert.Empty(result.Samples);
        Assert.Equal(1f, result.Gain);
    }

    [Fact]
    public void Normalize_OutputNeverExceedsFullScale()
    {
        var input = new float[10000];
        var rnd = new Random(42);
        for (int i = 0; i < input.Length; i++) input[i] = (float)(rnd.NextDouble() * 2 - 1) * 0.001f;

        var result = Normalizer.Normalize(input);

        foreach (var s in result.Samples)
            Assert.InRange(s, -1.0f, 1.0f);
    }
}
