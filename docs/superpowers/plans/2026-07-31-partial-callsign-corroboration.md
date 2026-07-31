# Partial-callsign corroboration — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the conversation resolver recover a vessel when STT garbles some phonetic
letters of a spelled-out callsign, by matching the surviving characters as a pattern and
requiring the vessel's name to independently corroborate the hit.

**Architecture:** Three additions, each in the module that already owns that concern.
`corrections.py` decodes a partial callsign into a regex pattern (pure function, no state).
`ais.py` looks the pattern up in the callsign cache and returns a vessel only if exactly one
matches. `conversations.py` adds a third candidate pass that combines the two and requires a
name to corroborate before offering the vessel to Claude, which still adjudicates.

**Tech Stack:** Python 3.10+, `rapidfuzz`, `pytest`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-31-partial-callsign-corroboration-design.md`

## Global Constraints

- Read module-owned mutable state **through the module** (`ais._callsign_cache`), never via
  an imported name. See `CONTRIBUTING.md`. Tests patch the owning module.
- Every new name that tests reach must be re-exported in `whisper-proxy.py`; the suite calls
  everything as `proxy.<name>`.
- Minimum known characters: **3**. Name corroboration: **`fuzz.ratio >= 60`**. Max pattern
  length: **7**. Max consecutive wildcards: **2**. These are measured values — do not adjust.
- Ambiguity returns `None`. A pattern matching several callsigns is not an identification.
- Rollback switch `AIS_PARTIAL_CALLSIGN=off` must restore current behaviour exactly.
- Run the whole suite with `py -m pytest server/tests -q` from the repo root.
- CI gate: `python -m pyflakes server/` must report no `undefined name`.

---

### Task 1: Decode a partial callsign into a pattern

**Files:**
- Modify: `server/stt_proxy/corrections.py` (add beside `_spelled_out_runs`, ~line 205)
- Test: `server/tests/test_whisper_proxy.py` (add after `test_spelled_out_runs_break_on_ordinary_words`)

**Interfaces:**
- Consumes: `_PHONETIC_LETTERS`, `_SPOKEN_DIGITS` (already in `corrections.py`)
- Produces: `_decode_spoken_word(word: str) -> str | None`,
  `_partial_callsign_pattern(text: str) -> tuple[str, int] | None` returning
  `(regex_pattern, known_character_count)`

- [ ] **Step 1: Write the failing tests**

```python
def test_partial_callsign_pattern_decodes_the_real_transmission():
    """MSC TEMA VIII (5LRK9) went unidentified: Whisper heard Lima->'DEMA', Kilo->'clear'."""
    text = "Good afternoon, this is Motortanker MSC DEMA eight, Callsign five DEMA Romeo, clear nine."
    assert proxy._partial_callsign_pattern(text) == ("5.R.9", 3)


def test_partial_callsign_pattern_is_anchored_on_the_keyword():
    """Unanchored, 'eight' from the vessel name leaks in and yields '8.5.R.9'."""
    assert proxy._partial_callsign_pattern("Motortanker MSC DEMA eight, five DEMA Romeo, clear nine") is None


# Each case must be rejected by the rule it names and no other, or it proves nothing.
# "Callsign Oscar Whiskey" for instance is fully decoded, so it would be refused by the
# all-decoded branch long before the minimum-known rule ever ran.
@pytest.mark.parametrize("text,rejected_by", [
    ("Callsign Oscar dema Whiskey",                    "2 known characters, floor is 3"),
    ("Callsign five dema clear kilos Romeo nine",      "3 consecutive wildcards, max is 2"),
    ("Callsign one two three dema four five six seven", "8 characters, ITU max is 7"),
    ("Callsign Zulu Charlie Foxtrot seven, over",      "fully decoded: the exact path owns it"),
    ("Maas Approach, Wilson Durness calling",          "no callsign keyword"),
    ("", "empty"),
    (None, "None"),
])
def test_partial_callsign_pattern_rejects_what_it_cannot_use(text, rejected_by):
    assert proxy._partial_callsign_pattern(text) is None, rejected_by


def test_partial_callsign_pattern_counts_one_wildcard_per_unreadable_word():
    got, known = proxy._partial_callsign_pattern("Callsign five dema Romeo clear nine")
    assert got == "5.R.9" and known == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest server/tests -q -k partial_callsign_pattern`
Expected: FAIL — `AttributeError: module 'whisper_proxy' has no attribute '_partial_callsign_pattern'`

- [ ] **Step 3: Extract the shared word decoder**

Replace the body of `_spelled_out_runs` in `server/stt_proxy/corrections.py` so both
functions decode a word the same way:

```python
def _decode_spoken_word(word: str) -> str | None:
    """The characters a single spoken word stands for, or None if it is ordinary speech."""
    char = _PHONETIC_LETTERS.get(word) or _SPOKEN_DIGITS.get(word)
    if char is None and word.isalnum():
        # Already-compact forms the decoder sometimes emits whole ("9HF5093"), and
        # single spoken characters ("9 Hotel Alpha").
        if len(word) == 1 or (len(word) <= 8 and any(c.isdigit() for c in word)
                              and any(c.isalpha() for c in word)):
            char = word.upper()
    return char


def _spelled_out_runs(text: str) -> list[str]:
    """Unbroken runs of spelled-out characters: phonetic letters, spoken digits, literals."""
    runs, current = [], []
    for word in re.findall(r"[A-Za-z0-9'-]+", (text or "").lower()):
        char = _decode_spoken_word(word)
        if char:
            current.append(char)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs
```

- [ ] **Step 4: Run the existing callsign tests to prove the refactor is behaviour-neutral**

Run: `py -m pytest server/tests -q -k "callsign or spelled_out"`
Expected: PASS — the refactor must change nothing.

- [ ] **Step 5: Add the pattern decoder**

Append to `server/stt_proxy/corrections.py`, after `_callsign_supported_by_text`:

```python
# Partial callsigns
#
# A callsign survives STT only partly: "five Lima Romeo Kilo nine" (5LRK9) came through as
# "five DEMA Romeo, clear nine", so the exact lookup -- a dictionary hit, no fuzz -- could
# never fire, and the vessel went unidentified with its callsign spelled out twice.
#
# What the decoder can still recover is an ordered set of known characters plus the gaps
# between them, on the assumption that each unreadable word was one spoken character. That
# yields "5.R.9", which matches exactly one cached callsign.
#
# The keyword anchor is what makes this safe. Scanning the whole transmission picks up the
# "eight" in "MSC DEMA eight" and yields "8.5.R.9", which is wrong. Every spelled-out
# callsign in the reference corpus says "callsign" first, so requiring it costs little and
# bounds the span to something that really is a callsign.
_CALLSIGN_ANCHOR_RE = re.compile(r"\bcall\s?signs?\b", re.IGNORECASE)

PARTIAL_CALLSIGN_MIN_KNOWN = 3   # fewer characters than this does not discriminate
PARTIAL_CALLSIGN_MAX_LEN   = 7   # ITU callsign maximum
PARTIAL_CALLSIGN_MAX_GAP   = 2   # consecutive wildcards; beyond this the pattern is noise


def _partial_callsign_pattern(text: str) -> tuple[str, int] | None:
    """Regex for a partly-decodable spelled-out callsign, plus how many characters are known.

    None when the text carries no usable callsign span. Returns None for a *fully* decoded
    callsign too: that is the exact lookup's job, and this path exists only for the partial
    case.
    """
    match = _CALLSIGN_ANCHOR_RE.search(text or "")
    if not match:
        return None

    words   = re.findall(r"[A-Za-z0-9'-]+", text[match.end():].lower())
    decoded = [_decode_spoken_word(w) for w in words]

    first = next((i for i, c in enumerate(decoded) if c), None)
    if first is None:
        return None
    last = max(i for i, c in enumerate(decoded) if c)
    span = decoded[first:last + 1]

    if all(c for c in span):          # nothing was garbled -- not this function's problem
        return None

    gap = worst_gap = 0
    for char in span:
        gap = 0 if char else gap + 1
        worst_gap = max(worst_gap, gap)
    if worst_gap > PARTIAL_CALLSIGN_MAX_GAP:
        return None

    known  = sum(len(c) for c in span if c)
    length = sum(len(c) if c else 1 for c in span)
    if known < PARTIAL_CALLSIGN_MIN_KNOWN or length > PARTIAL_CALLSIGN_MAX_LEN:
        return None

    return "".join(re.escape(c) if c else "." for c in span), known
```

- [ ] **Step 6: Re-export for the tests**

In `server/whisper-proxy.py`, add to the `from stt_proxy.corrections import (...)` block
(around line 193), keeping the list alphabetical:

```python
    _decode_spoken_word,
    _partial_callsign_pattern,
```

- [ ] **Step 7: Run the full suite**

Run: `py -m pytest server/tests -q`
Expected: PASS, with the new tests included.

- [ ] **Step 8: Commit**

```bash
git add server/stt_proxy/corrections.py server/whisper-proxy.py server/tests/test_whisper_proxy.py
git commit -m "Decode a partly-garbled spelled-out callsign into a match pattern"
```

---

### Task 2: Look a pattern up in the callsign cache

**Files:**
- Modify: `server/stt_proxy/ais.py` (add `import re`; add function after `match_by_callsign`, ~line 220)
- Test: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: `_callsign_cache`, `_cache_lock` (module state in `ais.py`)
- Produces: `match_by_callsign_pattern(pattern: str) -> dict | None` — the single matching
  cache entry, or `None` for zero or several matches

- [ ] **Step 1: Write the failing tests**

```python
@pytest.fixture
def pattern_cache(monkeypatch):
    cache = {
        "5LRK9": {"name": "MSC TEMA VIII", "mmsi": "636024193", "callsign": "5LRK9"},
        "5LCP9": {"name": "SIKINOS",       "mmsi": "111111111", "callsign": "5LCP9"},
        "PABC":  {"name": "SERENADA",      "mmsi": "275545000", "callsign": "PABC"},
    }
    monkeypatch.setattr(ais, "_callsign_cache", cache)
    return cache


def test_pattern_match_returns_the_only_vessel_that_fits(pattern_cache):
    assert proxy.match_by_callsign_pattern("5.R.9")["name"] == "MSC TEMA VIII"


def test_pattern_match_refuses_when_several_vessels_fit(pattern_cache):
    """Ambiguity is not an identification -- 5.??9 fits both 5LRK9 and 5LCP9."""
    assert proxy.match_by_callsign_pattern("5L..9") is None


@pytest.mark.parametrize("pattern", ["9.Z.4", "", None, "["])
def test_pattern_match_returns_nothing_when_it_cannot_match(pattern, pattern_cache):
    """Includes a malformed pattern: never raise into the resolver."""
    assert proxy.match_by_callsign_pattern(pattern) is None


def test_pattern_match_is_anchored_at_both_ends(pattern_cache):
    """'PAB' must not match 'PABC' -- a callsign is matched whole or not at all."""
    assert proxy.match_by_callsign_pattern("PAB") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest server/tests -q -k pattern_match`
Expected: FAIL — `AttributeError: ... has no attribute 'match_by_callsign_pattern'`

- [ ] **Step 3: Implement**

Add `import re` to the imports at the top of `server/stt_proxy/ais.py` (after `import os`),
then add after `match_by_callsign`:

```python
def match_by_callsign_pattern(pattern: str) -> dict | None:
    """The one cached vessel whose callsign matches `pattern`, or None.

    Returns None when several match: a pattern that fits more than one ship carries no
    identification, and picking any of them would be a guess wearing evidence's clothes.
    """
    if not pattern:
        return None
    try:
        matcher = re.compile(f"^{pattern}$")
    except re.error:
        return None
    with _cache_lock:
        entries = list(_callsign_cache.items())
    found = None
    for callsign, entry in entries:
        if matcher.match(callsign):
            if found is not None:
                return None
            found = entry
    return found
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -m pytest server/tests -q -k pattern_match`
Expected: PASS

- [ ] **Step 5: Re-export for the tests**

In `server/whisper-proxy.py`, add to the `from stt_proxy.ais import (...)` block (line 143),
after `match_by_callsign`:

```python
    match_by_callsign_pattern,
```

- [ ] **Step 6: Run the full suite and the lint gate**

Run: `py -m pytest server/tests -q && python -m pyflakes server/ | grep "undefined name"`
Expected: tests PASS; grep prints nothing.

- [ ] **Step 7: Commit**

```bash
git add server/stt_proxy/ais.py server/whisper-proxy.py server/tests/test_whisper_proxy.py
git commit -m "Look up a vessel by partial callsign pattern, refusing ambiguity"
```

---

### Task 3: Offer corroborated partial matches to the resolver

**Files:**
- Modify: `server/stt_proxy/conversations.py` (imports; `RESOLVER_SYSTEM_PROMPT` ~line 236;
  `_resolver_candidates` ~line 252; `_render_resolver_input` ~line 282)
- Modify: `server/start-all.bat.template` and `server/start-all.bat` (rollback switch list)
- Test: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: `_partial_callsign_pattern` (Task 1), `match_by_callsign_pattern` (Task 2),
  `ais._hint_probes`, `rf_fuzz.ratio`
- Produces: candidate dicts carrying `via_partial_callsign: True` and
  `partial_pattern: str`

- [ ] **Step 1: Write the failing tests**

```python
_TEMA = {"name": "MSC TEMA VIII", "mmsi": "636024193", "callsign": "5LRK9", "type": 91}
_REAL_CALL = ("Good afternoon, this is Motortanker MSC DEMA eight, "
              "Callsign five DEMA Romeo, clear nine.")


@pytest.fixture
def partial_caches(monkeypatch):
    monkeypatch.setattr(ais, "_callsign_cache", {"5LRK9": _TEMA})
    monkeypatch.setattr(ais, "_vessel_cache", {"MSC TEMA VIII": _TEMA})
    return _TEMA


def test_partial_callsign_becomes_a_candidate_when_the_name_agrees(partial_caches):
    """The reported miss: MSC TEMA VIII was in the cache and offered as no candidate."""
    cands = proxy._resolver_candidates([_chunk(30, _REAL_CALL, cid=1)])
    assert [c["name"] for c in cands] == ["MSC TEMA VIII"]
    assert cands[0]["via_partial_callsign"] is True
    assert cands[0]["partial_pattern"] == "5.R.9"


def test_partial_callsign_is_refused_when_no_name_corroborates(partial_caches):
    """The pattern alone is a guess. Without a name that agrees, offer nothing."""
    text = "Maas Approach, this is Wilson Durness, Callsign five DEMA Romeo, clear nine."
    assert proxy._resolver_candidates([_chunk(30, text, cid=1)]) == []


def test_partial_callsign_does_not_override_an_exact_match(monkeypatch, partial_caches):
    """An exact callsign is stronger evidence and must keep its mark.

    Both spellings must reach the same MMSI for this to test anything: one turn spells the
    callsign cleanly, a later one garbles it, and the vessel must stay marked exact.
    """
    monkeypatch.setattr(ais, "_callsign_cache", {"5LRK9": _TEMA, "PABC": _TEMA})
    cands = proxy._resolver_candidates([
        _chunk(40, "callsign papa alpha bravo charlie", cid=1, callsign="PABC"),
        _chunk(30, _REAL_CALL, cid=2),
    ])
    assert len(cands) == 1
    assert cands[0]["via_callsign"] is True
    assert "via_partial_callsign" not in cands[0]


def test_partial_callsign_can_be_disabled(monkeypatch, partial_caches):
    monkeypatch.setattr(conversations, "AIS_PARTIAL_CALLSIGN", False)
    assert proxy._resolver_candidates([_chunk(30, _REAL_CALL, cid=1)]) == []


def test_partial_candidate_is_marked_in_the_resolver_prompt():
    text = proxy._render_resolver_input(
        [_chunk(30, _REAL_CALL, cid=1)],
        [{"name": "MSC TEMA VIII", "mmsi": "636024193", "callsign": "5LRK9",
          "via_partial_callsign": True, "partial_pattern": "5.R.9"}])
    assert "partial callsign 5.R.9, name corroborated" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -m pytest server/tests -q -k partial`
Expected: FAIL — `_resolver_candidates` returns `[]` and the prompt lacks the marker.

- [ ] **Step 3: Add the imports and the switch**

In `server/stt_proxy/conversations.py`, extend the existing import lines:

```python
from stt_proxy.ais import (_find_ais_hints, _get_ship_type_name, _hint_probes,
                           match_by_callsign, match_by_callsign_pattern)
from stt_proxy.corrections import _callsign_supported_by_text, _partial_callsign_pattern
```

Add beside the other module switches, above `_resolver_candidates`:

```python
# Partial-callsign corroboration
#
# A garbled callsign used to contribute nothing: the lookup is exact, so two wrong characters
# out of five meant no match, and identification fell through to name similarity, which
# picked the wrong ship. The surviving characters are worth something -- "5.R.9" fits exactly
# one cached callsign -- but a pattern alone is a guess, and a pattern that uniquely fits the
# wrong ship is a confident false identity, the failure that costs most here.
#
# Requiring the vessel's name to corroborate independently is what makes it safe. Measured
# over the cache by garbling real callsigns at 20% per spoken character (n=2000): uniqueness
# alone gives 916 right / 1 wrong, but fires on a wrong ship 8.0% of the time when the true
# vessel is not in the callsign table at all. Adding the name check: 0 wrong, and 0.0% in
# that same uncached case. The threshold is 60 because the reported transmission scores 66.7
# ("MSC DEMA" against "MSC TEMA VIII") and 75 would have rejected it.
#
# Set AIS_PARTIAL_CALLSIGN=off to disable this pass entirely.
AIS_PARTIAL_CALLSIGN            = os.environ.get("AIS_PARTIAL_CALLSIGN", "on").strip().lower() != "off"
PARTIAL_CALLSIGN_MIN_NAME_SCORE = int(os.environ.get("PARTIAL_CALLSIGN_MIN_NAME_SCORE", "60"))


def _name_corroborates(vessel_name: str, chunks: list[dict]) -> bool:
    """True when some name spoken anywhere in the window resembles `vessel_name`.

    Reuses _hint_probes rather than a second name extractor, so "a name worth looking up"
    has one definition in this codebase.
    """
    target = vessel_name.upper()
    for chunk in chunks:
        for probe in _hint_probes(chunk.get("text", "")):
            if rf_fuzz.ratio(probe, target) >= PARTIAL_CALLSIGN_MIN_NAME_SCORE:
                return True
    return False


def _partial_callsign_candidates(chunks: list[dict]) -> dict[str, dict]:
    """Vessels whose callsign fits a partly-decoded spelling AND whose name was spoken."""
    found: dict[str, dict] = {}
    if not AIS_PARTIAL_CALLSIGN:
        return found
    for chunk in chunks:
        decoded = _partial_callsign_pattern(chunk.get("text", ""))
        if not decoded:
            continue
        pattern, _known = decoded
        entry = match_by_callsign_pattern(pattern)
        if not entry or not entry.get("mmsi"):
            continue
        if not _name_corroborates(entry["name"], chunks):
            continue
        marked = dict(entry)
        marked["via_partial_callsign"] = True
        marked["partial_pattern"] = pattern
        found[entry["mmsi"]] = marked
    return found
```

- [ ] **Step 4: Add the third pass to `_resolver_candidates`**

At the end of `_resolver_candidates`, immediately before `return list(candidates.values())`:

```python
    # Weakest of the three, so it runs last and never displaces a stronger match.
    for mmsi, entry in _partial_callsign_candidates(chunks).items():
        if mmsi not in candidates:
            candidates[mmsi] = entry
```

- [ ] **Step 5: Mark it in the rendered prompt**

In `_render_resolver_input`, replace the `via_callsign` line:

```python
            if c.get("via_callsign"):
                bits.append("** via callsign, exact match **")
            elif c.get("via_partial_callsign"):
                bits.append(f"** partial callsign {c['partial_pattern']}, name corroborated **")
```

- [ ] **Step 6: Add the ranking rule to the prompt**

In `RESOLVER_SYSTEM_PROMPT`, insert after rule 4 and renumber the rest so the list reads
1-8:

```
4. A candidate marked "via callsign" was matched exactly on a spelled-out callsign. Trust it
   above any name similarity.
5. A candidate marked "partial callsign" was matched on the characters that survived a
   garbled spelling, and separately on a name spoken in the exchange. Two weak signals that
   agree: weaker than an exact callsign, stronger than name resemblance alone.
6. Shore stations (Maas Approach, Rotterdam VTS, Pilot) are never the vessel.
7. "evidence" is a short quote from the transmissions, or a one-line reason. Keep it factual.
8. Do NOT return transcriptions. You are identifying speakers, not transcribing.
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `py -m pytest server/tests -q -k partial`
Expected: PASS

- [ ] **Step 8: Document the rollback switch**

In **both** `server/start-all.bat.template` and `server/start-all.bat`, add to the
vessel-identification filter list:

```
:: AIS_PARTIAL_CALLSIGN - matches a vessel on a partly-garbled spelled-out callsign when a
::                      spoken name agrees; off restores exact-callsign matching only
:: set AIS_PARTIAL_CALLSIGN=off
```

- [ ] **Step 9: Run the full suite and the lint gate**

Run: `py -m pytest server/tests -q && python -m pyflakes server/ | grep "undefined name"`
Expected: tests PASS; grep prints nothing.

- [ ] **Step 10: Commit**

```bash
git add server/stt_proxy/conversations.py server/tests/test_whisper_proxy.py \
        server/start-all.bat.template server/start-all.bat
git commit -m "Identify a vessel from a partial callsign when its name corroborates"
```

---

### Task 4: End-to-end check against the real cache

**Files:**
- Test: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3. Produces nothing new.

- [ ] **Step 1: Write the regression test**

```python
def test_the_reported_conversation_now_yields_a_candidate(monkeypatch):
    """12:09 on 2026-07-31: MSC TEMA VIII spelled its callsign out and the resolver was
    handed an empty candidate list, so 'unidentified' was the only answer available."""
    monkeypatch.setattr(ais, "_callsign_cache", {"5LRK9": _TEMA})
    monkeypatch.setattr(ais, "_vessel_cache", {"MSC TEMA VIII": _TEMA})
    window = [
        _chunk(60, "Maas Approach, Maas Approach, MST, FEMA 8, good afternoon sir.", cid=1),
        _chunk(50, _REAL_CALL, cid=2),
        _chunk(40, "Maas Approach, bring your message.", cid=3),
    ]
    rendered = proxy._render_resolver_input(window, proxy._resolver_candidates(window))
    assert "MSC TEMA VIII (MMSI:636024193)" in rendered
    assert "partial callsign 5.R.9" in rendered
    assert "(none" not in rendered
```

- [ ] **Step 2: Run it**

Run: `py -m pytest server/tests -q -k reported_conversation`
Expected: PASS (Tasks 1-3 already implement it; this pins the whole path together).

- [ ] **Step 3: Verify against the live cache by hand**

Run from `server/`:

```bash
py -c "
import json,sys,datetime; sys.path.insert(0,'.')
from stt_proxy import ais
from stt_proxy.conversations import _resolver_candidates, _render_resolver_input
e=json.load(open('ais_cache.json',encoding='utf-8'))
ais._vessel_cache={v['name'].upper():v for v in e}
ais._callsign_cache={v['callsign'].upper():v for v in e if v.get('callsign')}
T=['Maas Approach, Maas Approach, MST, FEMA 8, good afternoon sir.',
   'Good afternoon, this is Motortanker MSC DEMA eight, Callsign five DEMA Romeo, clear nine.']
ch=[{'id':i,'text':t,'callsign':None,'time':datetime.datetime.now()} for i,t in enumerate(T)]
print(_render_resolver_input(ch,_resolver_candidates(ch)))
"
```

Expected: the candidate block lists `MSC TEMA VIII (MMSI:636024193) cs:5LRK9
type:Container ship ** partial callsign 5.R.9, name corroborated **`.

If it does not, stop and diagnose before continuing — the unit tests use a three-entry
cache, and this is the only step that exercises the real 7,600-vessel one, where a pattern
that looked unique may not be.

- [ ] **Step 4: Confirm no regression in the rest of the identification path**

Run: `py -m pytest server/tests -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add server/tests/test_whisper_proxy.py
git commit -m "Pin the reported MSC TEMA VIII conversation end to end"
```

---

## Self-review

**Spec coverage.** Every section maps to a task: the `corrections.py` decoder and anchoring
rules to Task 1; `ais.py` pattern lookup and the ambiguity rule to Task 2; the
`conversations.py` third pass, corroboration, prompt rule, marking and rollback switch to
Task 3; the end-to-end regression and the live-cache check to Task 4. All five spec
thresholds appear as named constants with the spec's values. The three "Known limitations"
need no code — the keyword requirement is implemented in Task 1 Step 5, and the other two
are inherent.

**Type consistency.** `_partial_callsign_pattern` returns `tuple[str, int] | None`
throughout; Task 3 unpacks it as `pattern, _known`. `match_by_callsign_pattern` returns
`dict | None` and Task 3 checks for falsiness and `mmsi` before use. `_TEMA` and `_REAL_CALL`
are defined once in Task 3 and reused in Task 4, which runs after it.

**Deliberate omission.** No test asserts the exact `RESOLVER_SYSTEM_PROMPT` wording. The
suite does not pin prompt text anywhere, and doing so here would make ordinary prompt
tuning fail the build.
