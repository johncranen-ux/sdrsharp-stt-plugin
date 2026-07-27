using System;
using System.Collections.Generic;

namespace SDRSharp.SttPlugin.Dsp
{
    public sealed class VadConfig
    {
        public double SampleRate = 48_000;
        public int FrameMs = 20;
        public int SilenceMs = 600;              // trailing hangover before closing a segment
        public int PreRollMs = 400;               // audio retained before a confirmed onset
        public int TrailingKeepMs = 250;           // trailing silence actually kept in the sent chunk
        public int OnsetConfirmFrames = 3;         // N
        public int OnsetWindowFrames = 5;          // of M
        public int MaxSpeechSec = 30;
        public int MinSpeechMs = 300;              // minimum active speech before a chunk is sent
        public float MinActiveRatio = 0.20f;
        public float AbsoluteRmsFloor = 0.010f;    // never trigger below this regardless of noise floor
        public float OpenRatioAboveNoiseFloor = 3.0f;
        public float CloseRatioAboveNoiseFloor = 1.5f; // lower than open ratio: hysteresis
        public int NoiseFloorWindowMs = 3000;
        public float NoiseFloorPercentile = 0.20f;
    }

    public sealed class VadChunk
    {
        public float[] Samples = Array.Empty<float>();
        public float ActiveRatio;
        public float AvgRms;
    }

    // Pure frame-driven VAD state machine: feed 20ms (or FrameMs) frames in, get completed
    // chunks out. No I/O, no threading, so it can be replayed against captured audio in
    // tests without spinning up the plugin's queue/consumer plumbing.
    public sealed class VoiceActivityDetector
    {
        private readonly VadConfig _cfg;

        private readonly List<float> _speechBuf = new List<float>();
        private readonly Queue<float[]> _preRoll = new Queue<float[]>();
        private int _preRollSampleCount;

        private readonly List<float> _noiseFloorSamples = new List<float>();

        private readonly bool[] _onsetWindow;
        private int _onsetWindowPos;
        private int _onsetActiveCount;

        private bool _inSpeech;
        private int _silentFrames;
        private int _activeFrameCount;
        private int _totalFrameCount;

        public float LastRms     { get; private set; }
        public float NoiseFloor  { get; private set; }
        public bool  InSpeech    => _inSpeech;

        public VoiceActivityDetector(VadConfig cfg)
        {
            _cfg = cfg;
            _onsetWindow = new bool[Math.Max(1, cfg.OnsetWindowFrames)];
        }

        public void Reset()
        {
            _speechBuf.Clear();
            _preRoll.Clear();
            _preRollSampleCount = 0;
            _noiseFloorSamples.Clear();
            Array.Clear(_onsetWindow, 0, _onsetWindow.Length);
            _onsetWindowPos = 0;
            _onsetActiveCount = 0;
            _inSpeech = false;
            _silentFrames = 0;
            _activeFrameCount = 0;
            _totalFrameCount = 0;
            LastRms = 0f;
            NoiseFloor = 0f;
        }

        // Callers slice their raw sample stream into blocks of this size before calling
        // ProcessFrame; recompute after changing SampleRate/FrameMs.
        public int FrameSize     => Math.Max(1, (int)(_cfg.SampleRate * _cfg.FrameMs / 1000.0));
        private int SilenceFrames=> Math.Max(1, _cfg.SilenceMs / _cfg.FrameMs);
        private int PreRollFrames=> Math.Max(0, _cfg.PreRollMs / _cfg.FrameMs);
        private int NoiseFloorWindowFrames => Math.Max(1, _cfg.NoiseFloorWindowMs / _cfg.FrameMs);
        private int MinSpeechFrames => Math.Max(0, _cfg.MinSpeechMs / _cfg.FrameMs);

        // frame must be exactly FrameSize samples. squelchOpen: null when SDR#'s squelch is
        // unavailable/disabled, in which case the adaptive RMS gate is used instead.
        public VadChunk? ProcessFrame(float[] frame, bool? squelchOpen)
        {
            float rms = Rms(frame);
            LastRms = rms;

            return _inSpeech ? ProcessSpeechFrame(frame, rms, squelchOpen)
                              : ProcessIdleFrame(frame, rms, squelchOpen);
        }

        private VadChunk? ProcessIdleFrame(float[] frame, float rms, bool? squelchOpen)
        {
            float openThreshold = Math.Max(_cfg.AbsoluteRmsFloor, NoiseFloor * _cfg.OpenRatioAboveNoiseFloor);
            bool active = squelchOpen ?? (rms >= openThreshold);

            // Only track ambient level from frames already classified as quiet — otherwise
            // a loud onset frame would inflate its own threshold on a cold start.
            if (!active) UpdateNoiseFloor(rms);

            PushOnsetWindow(active);
            PushPreRoll(frame);

            if (_onsetActiveCount < _cfg.OnsetConfirmFrames)
                return null;

            // Onset confirmed: promote the pre-roll (which already contains the frames that
            // led to this confirmation) into the speech buffer.
            _inSpeech = true;
            _silentFrames = 0;
            _activeFrameCount = 0;
            _totalFrameCount = 0;
            _speechBuf.Clear();

            while (_preRoll.Count > 0)
            {
                var f = _preRoll.Dequeue();
                _speechBuf.AddRange(f);
                _totalFrameCount++;
            }
            _preRollSampleCount = 0;
            _activeFrameCount = _onsetActiveCount; // conservative: frames counted active within the onset window

            Array.Clear(_onsetWindow, 0, _onsetWindow.Length);
            _onsetWindowPos = 0;
            _onsetActiveCount = 0;

            return null;
        }

        private VadChunk? ProcessSpeechFrame(float[] frame, float rms, bool? squelchOpen)
        {
            float closeThreshold = Math.Max(_cfg.AbsoluteRmsFloor, NoiseFloor * _cfg.CloseRatioAboveNoiseFloor);
            bool activeForContinuing = squelchOpen ?? (rms >= closeThreshold);

            _speechBuf.AddRange(frame);
            _totalFrameCount++;
            if (activeForContinuing)
            {
                _activeFrameCount++;
                _silentFrames = 0;
            }
            else
            {
                _silentFrames++;
            }

            bool endOfSpeech = _silentFrames >= SilenceFrames;
            bool tooLong      = _speechBuf.Count >= (int)(_cfg.SampleRate * _cfg.MaxSpeechSec);

            if (!endOfSpeech && !tooLong)
                return null;

            var chunk = FinalizeChunk(endOfSpeech);

            if (endOfSpeech)
            {
                _inSpeech = false;
                _silentFrames = 0;
                _speechBuf.Clear();
                UpdateNoiseFloor(rms); // resume tracking ambient level immediately
            }
            else
            {
                // Long transmission: flush and keep listening without closing the segment.
                _speechBuf.Clear();
                _silentFrames = 0;
                _activeFrameCount = 0;
                _totalFrameCount = 0;
            }

            return chunk;
        }

        private VadChunk? FinalizeChunk(bool endOfSpeech)
        {
            var samples = _speechBuf;
            int frameSize = FrameSize;

            if (endOfSpeech)
            {
                // Trim the hangover down to TrailingKeepMs instead of shipping the full
                // SilenceMs of near-silent tail audio.
                int keepFrames = Math.Max(0, _cfg.TrailingKeepMs / _cfg.FrameMs);
                int trimFrames = Math.Max(0, Math.Min(_silentFrames, SilenceFrames) - keepFrames);
                int trimSamples = Math.Min(samples.Count, trimFrames * frameSize);
                if (trimSamples > 0)
                    samples.RemoveRange(samples.Count - trimSamples, trimSamples);
            }

            float activeRatio = _totalFrameCount > 0 ? (float)_activeFrameCount / _totalFrameCount : 0f;
            int activeSpeechMs = _activeFrameCount * _cfg.FrameMs;

            if (endOfSpeech && (activeSpeechMs < _cfg.MinSpeechMs || activeRatio < _cfg.MinActiveRatio))
                return null; // reject: squelch click / noise burst, not real speech

            var array = samples.ToArray();
            return new VadChunk
            {
                Samples = array,
                ActiveRatio = activeRatio,
                AvgRms = Rms(array),
            };
        }

        private void PushOnsetWindow(bool active)
        {
            if (_onsetWindow[_onsetWindowPos]) _onsetActiveCount--;
            _onsetWindow[_onsetWindowPos] = active;
            if (active) _onsetActiveCount++;
            _onsetWindowPos = (_onsetWindowPos + 1) % _onsetWindow.Length;
        }

        private void PushPreRoll(float[] frame)
        {
            _preRoll.Enqueue(frame);
            _preRollSampleCount += frame.Length;

            int capacitySamples = PreRollFrames * frame.Length;
            while (_preRollSampleCount > capacitySamples && _preRoll.Count > 0)
            {
                var removed = _preRoll.Dequeue();
                _preRollSampleCount -= removed.Length;
            }
        }

        private void UpdateNoiseFloor(float rms)
        {
            _noiseFloorSamples.Add(rms);
            int windowFrames = NoiseFloorWindowFrames;
            if (_noiseFloorSamples.Count > windowFrames)
                _noiseFloorSamples.RemoveAt(0);

            if (_noiseFloorSamples.Count == 0) { NoiseFloor = 0f; return; }

            var sorted = _noiseFloorSamples.ToArray();
            Array.Sort(sorted);
            int idx = (int)(_cfg.NoiseFloorPercentile * (sorted.Length - 1));
            NoiseFloor = sorted[Math.Clamp(idx, 0, sorted.Length - 1)];
        }

        private static float Rms(float[] samples)
        {
            if (samples.Length == 0) return 0f;
            double sum = 0;
            foreach (var s in samples) sum += s * (double)s;
            return (float)Math.Sqrt(sum / samples.Length);
        }
    }
}
