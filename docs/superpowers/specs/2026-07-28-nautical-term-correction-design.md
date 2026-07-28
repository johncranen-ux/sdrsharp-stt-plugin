# Nautical-term correction pass — design

## Goal

Extend the existing regex-based STT correction pass (`_apply_sttt_corrections` in
`server/whisper-proxy.py`) with additional patterns for recurring, consistent
nautical-term mis-transcriptions, using real evidence from the 49-clip hand-transcribed
benchmark set rather than guessing. This is the deferred "nautical-term correction" item
from `CLAUDE.md`'s "Additional Features" section — the lightweight regex-list scope, not
the larger future "local LLM correction" idea.

## Data source

- Clips: `D:\SDR\SDRSharp\Plugins\SttPlugin\captures\2026-07-27\` (49 usable clips, 2
  Dutch clips excluded)
- Ground truth: `server/references.txt`
- Config: `beam5_prompt` in `server/bench.py` — the shipped production decoder settings
  (large-v3, beam_size=5, best_of=5, maritime prompt, VAD off)

## Steps

1. Run `bench.py` against the current server (large-v3 + `--no-flash-attn`, slightly
   different from what was benchmarked last session) to get fresh hypothesis/reference
   pairs as JSON.
2. Extend `word_error_counts` in `bench.py` (currently returns only edit distance) to
   also return the actual substitution/insertion/deletion operations via Levenshtein
   traceback, so the real mis-transcription pairs can be extracted, not just a WER
   percentage.
3. Aggregate substitution pairs across all 49 clips, sorted by frequency.
4. Hand-review the list. Add only patterns that are: recurring (seen more than once),
   consistent (same direction every time), and maritime/nautical-specific (not generic
   grammar noise). Add each as a new rule in `_apply_sttt_corrections`, following the
   existing style (word-boundary regex, case-insensitive).
5. Add one pytest case per new rule in `server/tests/`.
6. Re-run the same benchmark and confirm pooled WER improves versus the pre-change
   baseline from step 1. This is the acceptance check — not "looks plausible."
7. Restart the proxy to deploy. Update the README's WER table if the number moved
   meaningfully.

## Explicitly out of scope

- The local-LLM-based correction idea from CLAUDE.md's future-features list.
- Vessel-name correction (already built, separate system — AIS fuzzy-match + Claude
  extraction).
- Re-running the model/VAD/beam-search comparisons already decided last session.

## Success criteria

- New correction rules are each backed by an observed, repeated pattern in the reference
  data, not speculation.
- Pooled WER on the 49-clip set does not regress, and ideally improves.
- All existing + new tests pass.
