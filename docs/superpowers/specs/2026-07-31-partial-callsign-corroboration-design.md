# Partial-callsign corroboration — design

## Problem

A vessel spelled its callsign out and went unidentified. From the 2026-07-31 log:

```
[12:09:18] CH01: vessel=MSC TAMISHKA F  type=container  mmsi=636019364  cs=A8TT2/name
        Good afternoon, this is Motortanker MSC DEMA eight, Callsign five DEMA Romeo, clear nine.
  [callsign] dropped '5DEMA': not spelled out in the transmission
```

The speaker was **MSC TEMA VIII, callsign 5LRK9, MMSI 636024193** — in the AIS cache the
whole time. Whisper mangled two of the five spoken characters:

```
spoken :  five   Lima   Romeo   Kilo    nine     -> 5LRK9
heard  :  five   DEMA   Romeo   clear   nine
```

The vessel name went the same way: "MSC DEMA eight" is "MSC TEMA VIII" (eight = VIII).

What the pipeline did with it:

1. `_spelled_out_runs` recovered `['8', '5', 'R', '9']` — "DEMA" and "clear" are not
   phonetic words, so they broke the runs.
2. Claude proposed `5DEMA`; `_callsign_supported_by_text` correctly dropped it as not
   readable from the text. `5DEMA` is not a real callsign, so this guard did its job.
3. `match_by_callsign` is an exact dictionary lookup with no fuzzy matching, by design.
   Two wrong characters out of five means it can never hit.
4. Identification fell through to the name path, which returned a wrong vessel.

The callsign therefore contributed **nothing**, and `_resolver_candidates` returns zero
candidates for this conversation — so even the retrospective resolver cannot recover it.

The discarded information was not worthless. The decoder did recover position 1 = `5`,
position 3 = `R`, position 5 = `9`, and a length of 5. Against the cached callsigns (7,118 at
the time of measurement — the AIS feed grows the cache continuously) the pattern `5.R.9`
matches exactly one: `5LRK9`, MSC TEMA VIII.

## Goal

Recover the vessel when STT garbles some phonetic letters of a spelled-out callsign, without
ever putting a confident wrong identity on screen.

## Principle

A partial callsign alone is a guess. A garbled vessel name alone is a guess. Requiring the
two to agree turns two weak signals into evidence. This is the rule the rest of the pipeline
already runs on, applied to a new pair.

## Non-goals

- Does not change the live per-transmission pass (`identify.enrich_with_ais`).
- Does not change exact-callsign behaviour or `_callsign_supported_by_text`.
- Does not change how the resolver weighs candidates beyond one added prompt rule.
- Does not attempt partial matching when no callsign keyword is present (see Anchoring).

## Design

Three units, following the module ownership documented in `CONTRIBUTING.md`.

### 1. `stt_proxy/corrections.py` — phonetic decoding

```python
def _partial_callsign_pattern(text: str) -> tuple[str, int] | None:
    """Regex pattern for a partly-decodable spelled-out callsign, and how many
    characters are known. None when the text has no usable callsign span."""
```

Pure function, no state. Sits beside `_spelled_out_runs`, which owns the same phonetic
table.

Algorithm:

1. Find the anchor: the first word matching `callsign` or `call sign` (case-insensitive).
   If absent, return `None`.
2. Walk the words after the anchor. Each word that decodes through `_PHONETIC_LETTERS` or
   `_SPOKEN_DIGITS` yields its character; every other word yields one wildcard.
3. Trim to the span between the first and last decoded character, so leading and trailing
   speech is dropped.
4. Reject and return `None` if any of: fewer than 3 known characters; total length > 7;
   more than 2 consecutive wildcards.
5. Return the regex pattern (`.` for each wildcard) and the known-character count.

### 2. `stt_proxy/ais.py` — pattern lookup

```python
def match_by_callsign_pattern(pattern: str) -> dict | None:
    """The one cached vessel whose callsign matches `pattern`, or None if zero or
    several match."""
```

Anchored regex over `_callsign_cache` keys, read through module-level state under
`_cache_lock` per the `CONTRIBUTING.md` rule. **Ambiguity returns `None` by design** — a
pattern matching several vessels carries no identification.

### 3. `stt_proxy/conversations.py` — candidate assembly

A third pass in `_resolver_candidates`, after the exact-callsign and hint passes. For each
chunk in the window:

1. `pattern, known = _partial_callsign_pattern(chunk["text"])` — skip if `None`.
2. `ais = match_by_callsign_pattern(pattern)` — skip if `None`.
3. Corroborate: require `fuzz.ratio(spoken_name, ais["name"]) >= 60` for at least one
   vessel name mentioned anywhere in the window, where candidate spoken names are the
   probes already produced by `ais._hint_probes`. Skip if nothing corroborates.

   Verified on the motivating transmission: `_hint_probes` yields `MSC DEMA`, which scores
   66.7 against `MSC TEMA VIII` and clears the threshold. Reusing `_hint_probes` rather than
   inventing a second name extractor keeps one definition of "a name worth looking up".
4. Add the entry marked `via_partial_callsign: True`, only if that MMSI is not already a
   candidate (an exact-callsign or hint match already present wins and is not overwritten).

Rendered by `_render_resolver_input` as:

```
  - MSC TEMA VIII (MMSI:636024193) cs:5LRK9 type:Container ship ** partial callsign 5.R.9, name corroborated **
```

One rule is added to `RESOLVER_SYSTEM_PROMPT`, ranking it below an exact callsign and above
name similarity: a partial-callsign candidate was matched on some spelled-out characters
plus an independently corroborating name, so it is stronger than name resemblance alone but
weaker than an exact callsign.

Claude adjudicates as it already does. Nothing here is auto-accepted.

## Anchoring

The decoder must not treat a whole transmission as a callsign. Measured on the real one:

| scan | pattern | outcome |
|---|---|---|
| whole transmission | `8.5.R.9` | wrong — the `8` leaks in from "MSC DEMA **eight**" |
| anchored after "Callsign" | `5.R.9` | resolves uniquely to `5LRK9` |

Requiring the keyword is deliberate and conservative. Every spelled-out callsign in
`references-2026-07-28.txt` carries it. It does skip cases like "this is Cosco Hope, nine
Victor eight seven eight six" — but that one already resolves through the exact path, so the
restriction narrows only the new, riskier route.

## Thresholds

All measured, not chosen. Method in the next section.

| Setting | Value | Evidence |
|---|---|---|
| minimum known characters | 3 | the motivating case has exactly 3 |
| name corroboration | `fuzz.ratio >= 60` | the real case scores 66.7; a threshold of 75 would reject it |
| uniqueness | required | `5.R.9` matches 1 of the 7,118 cached callsigns |
| maximum pattern length | 7 | ITU callsign maximum |
| maximum consecutive wildcards | 2 | beyond this the pattern stops discriminating |

## Measurement method

Reproduces the approach used for the name matcher. Ground truth is the AIS cache itself:
take a real callsign, render it as it would be spoken, mis-hear each spoken character
independently with probability *p*, decode, and check which vessel comes back.

Two failure modes are measured separately, because they differ in cost:

- **True vessel cached, pattern uniquely fits a different vessel.** Rare.
- **True vessel absent from the callsign table** (it may be out of AIS range, or cached
  without static data — roughly 500 cached vessels carry no callsign at any given time)
  **and the pattern uniquely fits some unrelated ship.** This is the dangerous one: a
  confident false identity, which is the failure this codebase weighs most heavily.

Results at *p* = 20% per spoken character, n = 2000:

| configuration | right | wrong | fires wrong when true vessel uncached |
|---|---|---|---|
| uniqueness only | 916 | 1 | **8.0%** |
| uniqueness + name corroboration ≥ 60 | 983 | 0 | **0.0%** |

At *p* = 40% the same pattern holds: corroboration takes wrong matches to 0 and the
uncached-vessel false-positive rate to 0.0%. Corroboration is what makes the feature safe;
uniqueness alone is not sufficient.

(Recall counts are not comparable across configurations — each row is an independent sample.
The `wrong` and uncached columns are the signal.)

## Testing

- **`corrections.py`**: decoding the motivating transmission yields `("5.R.9", 3)`;
  unanchored text and text with no keyword yield `None`; each rejection rule (too few known
  characters, over-length, too many consecutive wildcards) has a case.
- **`ais.py`**: a pattern matching exactly one cached callsign returns it; a pattern matching
  several returns `None`; a pattern matching none returns `None`.
- **`conversations.py`**: the MSC TEMA VIII transmission produces a candidate marked
  `via_partial_callsign`; a disagreeing vessel name suppresses it; an MMSI already present
  from the exact-callsign pass is not overwritten.
- **End-to-end regression**: the full 12:09 conversation yields MSC TEMA VIII as a candidate
  where it currently yields none.
- A measurement summary comment in `ais.py`, in the style of the existing name-matcher and
  hint-filter notes.

## Rollback

`AIS_PARTIAL_CALLSIGN=off` disables the third pass entirely, restoring current behaviour
exactly. Follows the existing `AIS_HINT_FILTER` / `AIS_NAME_FILTER` / `PROMPT_ECHO_FILTER`
convention, and is added to the documented list in `start-all.bat.template`.

## Known limitations

- Requires the word "callsign" in the transmission. A callsign spelled out without it is not
  attempted.
- Assumes each unrecognised word inside the span was exactly one spoken character. A speaker
  who says "double seven" or whom Whisper renders as two words for one letter produces a
  wrong-length pattern, which then matches nothing — a miss, not a false positive.
- Depends on AIS static data having supplied the callsign; 526 of the 7,644 cached vessels
  have no callsign and are unreachable by this path.
