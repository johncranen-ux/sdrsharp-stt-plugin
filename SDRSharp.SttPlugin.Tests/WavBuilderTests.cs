using System;
using System.Text;
using SDRSharp.SttPlugin;

namespace SDRSharp.SttPlugin.Tests;

public class WavBuilderTests
{
    [Fact]
    public void Build_WritesCanonicalHeader()
    {
        var samples = new float[] { 0f, 0.5f, -0.5f };
        var wav = WavBuilder.Build(samples, 16_000);

        Assert.Equal("RIFF", Encoding.ASCII.GetString(wav, 0, 4));
        Assert.Equal("WAVE", Encoding.ASCII.GetString(wav, 8, 4));
        Assert.Equal("fmt ", Encoding.ASCII.GetString(wav, 12, 4));

        short audioFormat = BitConverter.ToInt16(wav, 20);
        short numChannels = BitConverter.ToInt16(wav, 22);
        int   sampleRate  = BitConverter.ToInt32(wav, 24);
        short bitsPerSample = BitConverter.ToInt16(wav, 34);
        Assert.Equal(1, audioFormat);
        Assert.Equal(1, numChannels);
        Assert.Equal(16_000, sampleRate);
        Assert.Equal(16, bitsPerSample);

        Assert.Equal("data", Encoding.ASCII.GetString(wav, 36, 4));
        int dataSize = BitConverter.ToInt32(wav, 40);
        Assert.Equal(samples.Length * 2, dataSize);
        Assert.Equal(44 + dataSize, wav.Length);
    }

    [Fact]
    public void Build_ClipsOutOfRangeSamples()
    {
        var wav = WavBuilder.Build(new float[] { 2.0f, -2.0f }, 16_000);
        short s0 = BitConverter.ToInt16(wav, 44);
        short s1 = BitConverter.ToInt16(wav, 46);
        Assert.Equal(short.MaxValue, s0);
        Assert.Equal(short.MinValue, s1);
    }

    [Fact]
    public void Build_EmptyInput_ReturnsEmptyArray()
    {
        Assert.Empty(WavBuilder.Build(Array.Empty<float>(), 16_000));
    }

    [Fact]
    public void Resample_SameRate_ReturnsInputUnchanged()
    {
        var input = new float[] { 0.1f, 0.2f, 0.3f };
        var result = WavBuilder.Resample(input, 16_000, 16_000);
        Assert.Same(input, result);
    }

    [Fact]
    public void Resample_DownsamplesToExpectedLength()
    {
        var input = new float[48_000]; // 1 s at 48 kHz
        var result = WavBuilder.Resample(input, 48_000, 16_000);
        Assert.Equal(16_000, result.Length);
    }
}
