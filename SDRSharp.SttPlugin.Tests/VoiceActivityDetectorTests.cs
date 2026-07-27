using System;
using System.Linq;
using SDRSharp.SttPlugin.Dsp;

namespace SDRSharp.SttPlugin.Tests;

public class VoiceActivityDetectorTests
{
    // A slow, small sample rate keeps test arrays readable: FrameMs=20 at 1000 Hz is a
    // 20-sample frame. The VAD is sample-rate agnostic, so this is purely for test clarity.
    private static VadConfig BaseConfig() => new VadConfig
    {
        SampleRate = 1000,
        FrameMs = 20,             // 20 samples/frame
        PreRollMs = 100,          // 5 frames
        OnsetConfirmFrames = 2,
        OnsetWindowFrames = 3,
        SilenceMs = 100,          // 5 frames
        TrailingKeepMs = 0,
        MinSpeechMs = 0,
        MinActiveRatio = 0f,
        AbsoluteRmsFloor = 0.05f,
        OpenRatioAboveNoiseFloor = 3f,
        CloseRatioAboveNoiseFloor = 1.5f,
        NoiseFloorWindowMs = 3000,
    };

    private static float[] Frame(float value, int size = 20) => Enumerable.Repeat(value, size).ToArray();

    [Fact]
    public void Onset_RetainsPreRollAudio_NotJustTheTriggeringFrames()
    {
        var vad = new VoiceActivityDetector(BaseConfig());

        for (int i = 0; i < 10; i++) Assert.Null(vad.ProcessFrame(Frame(0f), null)); // silence
        Assert.Null(vad.ProcessFrame(Frame(0.5f), null)); // 1st active frame: not yet confirmed
        Assert.Null(vad.ProcessFrame(Frame(0.5f), null)); // 2nd active frame: onset confirmed
        Assert.True(vad.InSpeech);

        VadChunk? chunk = null;
        for (int i = 0; i < 5; i++) // silence long enough to close the segment
        {
            var c = vad.ProcessFrame(Frame(0f), null);
            if (c != null) chunk = c;
        }

        Assert.NotNull(chunk);
        // Pre-roll capacity is 5 frames; the confirming onset drains [3 silent, 2 active].
        Assert.Equal(100, chunk!.Samples.Length);
        Assert.All(chunk.Samples.Take(60), s => Assert.Equal(0f, s));
        Assert.All(chunk.Samples.Skip(60), s => Assert.Equal(0.5f, s, 3));
    }

    [Fact]
    public void SingleFrameSpike_DoesNotOpenSpeech()
    {
        var cfg = BaseConfig();
        cfg.OnsetConfirmFrames = 3;
        cfg.OnsetWindowFrames = 5;
        var vad = new VoiceActivityDetector(cfg);

        for (int i = 0; i < 5; i++) vad.ProcessFrame(Frame(0f), null);
        vad.ProcessFrame(Frame(0.5f), null); // one isolated loud frame (squelch click)
        for (int i = 0; i < 10; i++)
        {
            var chunk = vad.ProcessFrame(Frame(0f), null);
            Assert.Null(chunk);
        }
        Assert.False(vad.InSpeech);
    }

    [Fact]
    public void ShortBurst_BelowMinSpeechMs_IsRejected()
    {
        var cfg = BaseConfig();
        cfg.OnsetConfirmFrames = 1; // open immediately for this test
        cfg.OnsetWindowFrames = 1;
        cfg.MinSpeechMs = 200;      // require 10 frames of real activity
        var vad = new VoiceActivityDetector(cfg);

        vad.ProcessFrame(Frame(0.5f), null); // opens (1 active frame = 20ms of speech)
        Assert.True(vad.InSpeech);

        VadChunk? chunk = null;
        for (int i = 0; i < 5; i++) // close the segment
        {
            var c = vad.ProcessFrame(Frame(0f), null);
            if (c != null) chunk = c;
        }

        Assert.Null(chunk); // 20ms of activity is well under the 200ms floor
        Assert.False(vad.InSpeech); // state must still have reset, not gotten stuck

        // Prove the detector is not stuck: it can open again on the next real onset.
        vad.ProcessFrame(Frame(0.5f), null);
        Assert.True(vad.InSpeech);
    }

    [Fact]
    public void Hysteresis_KeepsSegmentOpenThroughMidLevelDip()
    {
        var cfg = BaseConfig();
        cfg.OnsetConfirmFrames = 1;
        cfg.OnsetWindowFrames = 1;
        cfg.AbsoluteRmsFloor = 0.02f;
        cfg.OpenRatioAboveNoiseFloor = 100f;   // open threshold effectively = AbsoluteRmsFloor
        cfg.CloseRatioAboveNoiseFloor = 100f;  // close threshold also = AbsoluteRmsFloor (noise floor ~0)
        var vad = new VoiceActivityDetector(cfg);

        vad.ProcessFrame(Frame(0.5f), null); // opens
        Assert.True(vad.InSpeech);

        // A dip that stays above the close threshold (0.02) must not count as silence.
        for (int i = 0; i < 20; i++)
        {
            vad.ProcessFrame(Frame(0.03f), null);
            Assert.True(vad.InSpeech, "mid-level audio above the close threshold closed the segment early");
        }
    }

    [Fact]
    public void Squelch_OverridesRms_WhenProvided()
    {
        var cfg = BaseConfig();
        cfg.OnsetConfirmFrames = 2;
        cfg.OnsetWindowFrames = 2;
        var vad = new VoiceActivityDetector(cfg);

        // Loud audio but squelch reports closed: must not open.
        for (int i = 0; i < 5; i++) vad.ProcessFrame(Frame(0.9f), squelchOpen: false);
        Assert.False(vad.InSpeech);

        // Quiet audio but squelch reports open: must open anyway.
        vad.ProcessFrame(Frame(0.001f), squelchOpen: true);
        vad.ProcessFrame(Frame(0.001f), squelchOpen: true);
        Assert.True(vad.InSpeech);
    }

    [Fact]
    public void Reset_ClearsAllState()
    {
        var vad = new VoiceActivityDetector(BaseConfig());
        vad.ProcessFrame(Frame(0.5f), null);
        vad.ProcessFrame(Frame(0.5f), null);
        Assert.True(vad.InSpeech);

        vad.Reset();

        Assert.False(vad.InSpeech);
        Assert.Equal(0f, vad.LastRms);
    }
}
