"""Recent-traffic memory, and resolving who was speaking after an exchange ends.

Live identification sees one transmission and cannot revisit it, so a garbled opening call
is identified from the worst evidence available. This module keeps every transmission until
its channel goes quiet, then hands the whole exchange to Claude to segment and identify --
by which point the clear repeat, or the spelled-out callsign, has arrived.

The critical property is that `resolve_conversation`'s output schema has **no text field**.
An earlier attempt fed prior turns into the same call that produced the transcription and
they bled into it: fabrication nearly doubled. Deciding identity separately, after the text
is final, makes that impossible rather than merely discouraged. Do not add a text field.

Also renders the /conversations page, since that presentation only ever shows this data.
"""

import datetime
import json
import os
import re
import threading

from rapidfuzz import fuzz as rf_fuzz

from stt_proxy.ais import (_find_ais_hints, _get_ship_type_name, _hint_probes,
                           match_by_callsign, match_by_callsign_pattern, match_by_mmsi)
from stt_proxy.claude import _get_claude
from stt_proxy.corrections import (_callsign_supported_by_text, _partial_callsign_pattern,
                                   _spelled_out_runs)
# Re-exported: whisper-proxy.py and the tests reach _html_escape through this module.
from stt_proxy.markup import VESSELFINDER_URL, _html_escape, _vessel_link  # noqa: F401


# Conversation sessions
#
# A VHF exchange is normally one vessel alternating with the shore station. Identifying each
# chunk in isolation loses that, so a garbled first call is identified from the worst evidence
# available and never revisited.
#
# Feeding prior turns into the *same* Claude call that produces the transcription was tried
# and removed: measured over 249 real chunks it nearly doubled fabrication (18 -> 32 chunks
# returning words nobody said, e.g. "Copy that, thank you." coming back as "Gungor Star one
# three one five, correct.") and could propagate a wrong identity across a whole exchange.
# Context in the transcription call bleeds into the transcription, and two rounds of prompt
# tightening reduced but never stopped it.
#
# Identity is now resolved *after* a conversation ends, by a separate pass whose output schema
# has no text field at all -- see resolve_conversation().

# How long a recent identification stays available for the "Maas response" correlation in
# do_POST, which upgrades a fuzzy vessel match when a later turn names the vessel clearly.
VESSEL_BUFFER_TTL   = int(os.environ.get("VESSEL_BUFFER_TTL_S", "120"))

# ---------------------------------------------------------------------------
# Recent vessel identifications buffer
# ---------------------------------------------------------------------------

_vessel_buffer = []
_buffer_lock = threading.Lock()


def _is_maas_response(raw_text: str) -> bool:
    maas_indicators = [
        "maas approach", "rotterdam vts", "pilot", "approach roger",
        "roger that", ", roger", "understood", "wilco", "say again", "what is your",
    ]
    text_lower = raw_text.lower()
    return any(indicator in text_lower for indicator in maas_indicators)


def _add_to_buffer(result: dict, raw_text: str, channel: str = "",
                   when: datetime.datetime | None = None) -> None:
    # `when` exists so replay_sessions.py can re-run captured traffic against its original
    # timestamps; live callers leave it alone.
    with _buffer_lock:
        now = when or datetime.datetime.now()
        _vessel_buffer.append({
            "time": now,
            "vessel": result.get("vessel"),
            "fuzzy": result.get("match_method") == "name_fuzzy",
            "result": result,
            "raw_text": raw_text,
            "channel": channel,
            "shore": _is_maas_response(raw_text),
        })
        cutoff = now - datetime.timedelta(seconds=VESSEL_BUFFER_TTL)
        _vessel_buffer[:] = [e for e in _vessel_buffer if e["time"] > cutoff]



# ---------------------------------------------------------------------------
# Conversation journal and windowing
#
# Live identification sees one transmission and cannot revisit it, so a garbled first call
# is identified from the worst evidence available. These chunks are kept until the traffic
# on their channel goes quiet, then handed to resolve_conversation() which sees the whole
# exchange -- including the turn where the shore station repeats the name clearly, or asks
# for a callsign that settles it exactly.
#
# A window is a *container*, not a conversation. Measured on the 260-chunk 2026-07-28
# session, a 120s gap yields a median window of 11 chunks spanning 116s and a longest of 45
# chunks over 10 minutes: CH01 is shared, so Maas Approach works many vessels back-to-back
# and a 20s gap often means a different ship called. 60s is tighter (median 5 chunks, 39s)
# but still merges exchanges, so the resolver segments the window by content -- something no
# gap rule can do.
# ---------------------------------------------------------------------------

CONVERSATION_RESOLVER   = os.environ.get("CONVERSATION_RESOLVER", "on").strip().lower() != "off"
CONVERSATION_GAP_S      = int(os.environ.get("CONVERSATION_GAP_S", "60"))
CONVERSATION_MAX_CHUNKS = int(os.environ.get("CONVERSATION_MAX_CHUNKS", "40"))
CONVERSATION_POLL_S     = 10.0

_conversation_chunks: list[dict] = []
_conversation_lock = threading.Lock()
_chunk_seq = 0


def _record_chunk(channel: str, raw_text: str, result: dict,
                  when: datetime.datetime | None = None) -> dict:
    """Journal one transmission for later retrospective resolution."""
    global _chunk_seq
    with _conversation_lock:
        _chunk_seq += 1
        chunk = {
            "id": _chunk_seq,
            "time": when or datetime.datetime.now(),
            "channel": channel,
            # Raw feeds the resolver -- corrections can mask the very evidence it needs
            # (a mangled name is a clue). Corrected is what the operator saw, so that is
            # what the page shows.
            "text": raw_text,
            "corrected": result.get("text") or raw_text,
            "live_vessel": result.get("vessel"),
            "live_mmsi": result.get("mmsi"),
            "callsign": result.get("callsign"),
        }
        _conversation_chunks.append(chunk)
    return chunk


def _split_windows(chunks: list[dict]) -> list[list[dict]]:
    """Split time-ordered chunks wherever the silence exceeds CONVERSATION_GAP_S."""
    windows: list[list[dict]] = []
    for chunk in sorted(chunks, key=lambda c: c["time"]):
        if windows and (chunk["time"] - windows[-1][-1]["time"]).total_seconds() <= CONVERSATION_GAP_S \
                and len(windows[-1]) < CONVERSATION_MAX_CHUNKS:
            windows[-1].append(chunk)
        else:
            windows.append([chunk])
    return windows


def _take_closed_windows(now: datetime.datetime | None = None) -> list[list[dict]]:
    """Remove and return every window that is finished; leave open ones journalled.

    A window is finished when a newer window exists on the same channel, when it has hit
    CONVERSATION_MAX_CHUNKS (bounding the resolver prompt), or when nothing has been heard
    on that channel for CONVERSATION_GAP_S.
    """
    now = now or datetime.datetime.now()
    taken: list[list[dict]] = []
    keep:  list[dict] = []

    with _conversation_lock:
        by_channel: dict[str, list[dict]] = {}
        for chunk in _conversation_chunks:
            by_channel.setdefault(chunk["channel"], []).append(chunk)

        for chunks in by_channel.values():
            windows = _split_windows(chunks)
            for i, window in enumerate(windows):
                superseded = i < len(windows) - 1
                full       = len(window) >= CONVERSATION_MAX_CHUNKS
                quiet      = (now - window[-1]["time"]).total_seconds() > CONVERSATION_GAP_S
                if superseded or full or quiet:
                    taken.append(window)
                else:
                    keep.extend(window)

        _conversation_chunks[:] = sorted(keep, key=lambda c: c["time"])

    return sorted(taken, key=lambda w: w[0]["time"])


def _find_fuzzy_match_in_buffer(vessel_name: str) -> tuple:
    if not vessel_name:
        return None, -1
    with _buffer_lock:
        now = datetime.datetime.now()
        cutoff = now - datetime.timedelta(seconds=VESSEL_BUFFER_TTL)
        for i in range(len(_vessel_buffer) - 1, -1, -1):
            entry = _vessel_buffer[i]
            if entry["time"] <= cutoff:
                break
            if not entry.get("fuzzy"):
                continue
            old_vessel = entry.get("vessel")
            if old_vessel and old_vessel.lower() != vessel_name.lower():
                similarity = rf_fuzz.token_set_ratio(old_vessel.lower(), vessel_name.lower())
                if similarity >= 50:
                    return entry, i
    return None, -1


def _update_buffer_entry(index: int, new_vessel: str, new_result: dict) -> None:
    with _buffer_lock:
        if 0 <= index < len(_vessel_buffer):
            _vessel_buffer[index]["vessel"] = new_vessel
            _vessel_buffer[index]["result"]["vessel"] = new_vessel
            _vessel_buffer[index]["fuzzy"] = False


# ---------------------------------------------------------------------------
# Retrospective conversation resolution
#
# Runs after a window closes, so the transcriptions are already final. Its output schema has
# NO text field: this pass physically cannot alter what was said, which is the difference
# between it and the forward-context approach that was tried and removed (that one shared a
# call with the transcription and nearly doubled fabrication).
#
# It picks from a candidate list assembled from AIS rather than naming vessels freely -- the
# same reasoning that forced the hint filter: given an open field it will match ordinary
# speech to a real ship.
# ---------------------------------------------------------------------------

RESOLVER_SYSTEM_PROMPT = """\
You are given consecutive VHF radio transmissions from one channel near Rotterdam
(Maas Approach / Rotterdam VTS), in time order, already transcribed.

They may contain SEVERAL separate exchanges: this is a shared working channel, so one vessel
finishes and another calls in shortly after. An exchange typically opens with a vessel
calling the shore station ("Maas Approach, Maas Approach, <name>") and then alternates
between that vessel and the shore station.

Split the transmissions into exchanges and identify the vessel in each.

Return ONLY raw JSON, no markdown:
{"exchanges": [{"chunk_ids": [1,2,3], "vessel": "<name or null>", "mmsi": "<mmsi or null>",
                "evidence": "<short quote or reason>", "confidence": "high|medium|low"}]}

Rules:
1. Every chunk id you were given must appear in exactly one exchange.
2. Choose "vessel" from the [CANDIDATES] list, copying the name exactly, or return null.
   Never invent a name and never use one that is not in the list. If the transmissions do
   not identify anyone, null is the correct answer.
3. Prefer the clearest evidence anywhere in the exchange over the first mention. A garbled
   opening call ("Selenada") is resolved by a later clear one, by the shore station repeating
   the name, or best of all by a spelled-out callsign.
4. A candidate marked "via callsign" was matched exactly on a spelled-out callsign. Trust it
   above any name similarity.
5. A candidate marked "partial callsign" was matched on the characters that survived a
   garbled spelling, and separately on a name spoken in the exchange. Two weak signals that
   agree: weaker than an exact callsign, stronger than name resemblance alone.
6. A candidate marked "live pass" is what the per-transmission pass matched for a single
   turn, before the rest of the exchange was known. Treat it as a lead, not as evidence: it
   is often wrong on a garbled opening call, which is why this pass exists. Weigh it against
   the transmissions like any other candidate.
7. Shore stations (Maas Approach, Rotterdam VTS, Pilot) are never the vessel.
8. "evidence" is a short quote from the transmissions, or a one-line reason. Keep it factual.
9. Do NOT return transcriptions. You are identifying speakers, not transcribing.
"""


# Candidates from the live pass
#
# The live pass already matched a vessel against the whole AIS cache using the complete
# extracted name. This function ignored that and rebuilt its list from unigram and bigram
# probes, which is strictly less information -- a three-word name cannot even be probed
# whole. "Santa Isabel Maas" hinted ISABEL at 100, an exact match on one substring word,
# while SANTA ISABEL MAERSK -- the ship actually calling, cached the whole time -- reached
# only 77.4 as the bigram "SANTA ISABEL", under a cutoff of 85. The resolver was handed a
# list without the right ship on it and, told to choose only from the list, picked ISABEL
# and called it high confidence. It was right to; the list was wrong.
#
# Measured over 24 stored conversations that had a live match, that vessel was missing from
# the candidate list in 9: 7 resolved to nobody, 2 resolved to a different ship. (Counted
# after discarding live values of three characters or fewer, which are artifacts of the
# WRatio bug fixed the same day and would no longer be produced.)
#
# This adds a candidate, never a verdict. A live guess is frequently wrong on a garbled
# opening call -- that is the entire reason this pass exists -- so it is marked as a lead
# and weighed against the exchange like anything else.
#
# Set RESOLVER_LIVE_CANDIDATES=off to restore the previous behaviour.
RESOLVER_LIVE_CANDIDATES = os.environ.get("RESOLVER_LIVE_CANDIDATES", "on").strip().lower() != "off"


def _live_match_candidates(chunks: list[dict]) -> dict[str, dict]:
    """Vessels the per-transmission pass already matched against AIS in this window."""
    found: dict[str, dict] = {}
    if not RESOLVER_LIVE_CANDIDATES:
        return found
    for chunk in chunks:
        mmsi = chunk.get("live_mmsi")
        if not mmsi or mmsi in found:
            continue
        entry = match_by_mmsi(mmsi)
        if not entry:
            continue
        marked = dict(entry)
        marked["via_live_match"] = True
        found[mmsi] = marked
    return found


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


# Spelled-out callsigns, read from the transmissions rather than from the journal
#
# This pass used to read only chunk["callsign"], which the live pass wrote -- so a live pass
# that recorded the wrong callsign, or none, took the exact lookup down with it. That is how
# PECHORA STAR was lost: the journal held VIKTORIA's DB6442, the guard below correctly refused
# it, and 9HA2788 was sitting in the transmission text the whole time. MONA SWAN was lost the
# other way, with no callsign extracted at all. The text is the primary source and is stored
# verbatim, so decode from that and the retrospective pass stops depending on the live guess
# it exists to second-guess.
#
# Whole runs only, never substrings. Measured over the 435 stored transmissions, whole-run
# matching finds all seven real callsigns (PECHORA STAR, MONA SWAN, ECO ROYALTY, CENTURIUS,
# COSCO HOPE, VENETIA, CORAL METHANE) with nothing spurious, three of them in transmissions
# that never say the word "callsign" -- "this is Cosco Hope, nine Victor eight seven eight
# six". Substring search adds no real vessel and opens a real hole: 239 of the 380 runs are
# times, draughts, channels and positions, and the cache holds all-digit transponder junk
# ('2503', '2603', '303') that a long spoken number would eventually hit. All-digit runs are
# skipped for the same reason, and four characters is the floor because the only shorter
# entries in the cache are junk ('AAA', '@L<').
CALLSIGN_RUN_MIN_LEN = 4


def _spoken_callsign_candidates(chunks: list[dict]) -> dict[str, dict]:
    """Vessels whose callsign was spelled out in the transmissions themselves."""
    found: dict[str, dict] = {}
    for chunk in chunks:
        for run in _spelled_out_runs(chunk.get("text", "")):
            if len(run) < CALLSIGN_RUN_MIN_LEN or run.isdigit():
                continue
            ais = match_by_callsign(run)
            if ais and ais.get("mmsi") and ais["mmsi"] not in found:
                entry = dict(ais)
                entry["via_callsign"] = True
                found[ais["mmsi"]] = entry
    return found


def _resolver_candidates(chunks: list[dict]) -> list[dict]:
    """AIS vessels plausibly involved in this window.

    Callsign matches come first and are marked: match_by_callsign is an exact dictionary
    lookup, so it is real evidence -- but only when the transmission actually spelled a
    callsign out. Otherwise the "exactness" is just an invented string that happened to
    exist, and the mark would launder a guess into evidence.
    """
    candidates: dict[str, dict] = dict(_spoken_callsign_candidates(chunks))

    for chunk in chunks:
        # Belt and braces: the live pass now drops unsupported callsigns, but a journal
        # written before that fix, or a future regression, must not promote one to evidence.
        if not _callsign_supported_by_text(chunk.get("callsign") or "", chunk.get("text", "")):
            continue
        ais = match_by_callsign(chunk.get("callsign") or "")
        if ais and ais.get("mmsi"):
            entry = dict(ais)
            entry["via_callsign"] = True
            candidates[ais["mmsi"]] = entry

    # Ahead of the hints: the live pass matched a complete extracted name against the whole
    # cache, where a hint is only a one- or two-word probe.
    for mmsi, entry in _live_match_candidates(chunks).items():
        if mmsi not in candidates:
            candidates[mmsi] = entry

    for chunk in chunks:
        for hint in _find_ais_hints(chunk.get("text", "")):
            mmsi = hint.get("mmsi")
            if mmsi and mmsi not in candidates:
                candidates[mmsi] = dict(hint)

    # Weakest of the three, so it runs last and never displaces a stronger match.
    for mmsi, entry in _partial_callsign_candidates(chunks).items():
        if mmsi not in candidates:
            candidates[mmsi] = entry

    return list(candidates.values())


def _render_resolver_input(chunks: list[dict], candidates: list[dict]) -> str:
    lines = ["[TRANSMISSIONS]"]
    for chunk in chunks:
        lines.append(f"  {chunk['id']}. [{chunk['time'].strftime('%H:%M:%S')}] {chunk.get('text', '')}")

    lines.append("")
    lines.append("[CANDIDATES]")
    if candidates:
        for c in candidates:
            bits = [f"{c['name']} (MMSI:{c['mmsi']})"]
            if c.get("callsign"):
                bits.append(f"cs:{c['callsign']}")
            if c.get("type"):
                bits.append(f"type:{_get_ship_type_name(c['type'])}")
            if c.get("via_callsign"):
                bits.append("** via callsign, exact match **")
            elif c.get("via_partial_callsign"):
                bits.append(f"** partial callsign {c['partial_pattern']}, name corroborated **")
            elif c.get("via_live_match"):
                bits.append("** live pass matched this name **")
            lines.append("  - " + " ".join(bits))
    else:
        lines.append("  (none -- every vessel must then be null)")
    return "\n".join(lines)


def resolve_conversation(chunks: list[dict]) -> list[dict]:
    """Segment a closed window into exchanges and identify each. Never returns text."""
    if not chunks:
        return []

    candidates = _resolver_candidates(chunks)
    by_name = {c["name"].upper(): c for c in candidates}

    try:
        client = _get_claude()
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=RESOLVER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _render_resolver_input(chunks, candidates)}],
        )
        content = message.content[0].text.strip()
        if "```" in content:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
            if m:
                content = m.group(1)
        exchanges = json.loads(content).get("exchanges", [])
    except Exception as exc:
        print(f"  [resolve error] {exc}", flush=True)
        return _unresolved(chunks)

    return _validate_exchanges(exchanges, chunks, by_name)


def _unresolved(chunks: list[dict]) -> list[dict]:
    """Fallback: one exchange, nobody identified. Never loses a transmission."""
    return [{"chunk_ids": [c["id"] for c in chunks], "vessel": None, "mmsi": None,
             "evidence": "resolver unavailable", "confidence": "low"}]


def _validate_exchanges(exchanges: list, chunks: list[dict], by_name: dict) -> list[dict]:
    """Keep the model inside the candidate list and account for every transmission.

    A name outside [CANDIDATES] is dropped rather than trusted: free-form naming is exactly
    how ordinary speech turned into real ships before the hint filter was tightened.
    """
    valid_ids = {c["id"] for c in chunks}
    seen: set[int] = set()
    out: list[dict] = []

    for ex in exchanges if isinstance(exchanges, list) else []:
        ids = [i for i in ex.get("chunk_ids", []) if i in valid_ids and i not in seen]
        if not ids:
            continue
        seen.update(ids)

        name = (ex.get("vessel") or "").strip()
        ais  = by_name.get(name.upper())
        if name and not ais:
            print(f"  [resolve] dropped off-list vessel {name!r}", flush=True)
        out.append({
            "chunk_ids": sorted(ids),
            "vessel": ais["name"] if ais else None,
            "mmsi": ais.get("mmsi") if ais else None,
            "callsign": ais.get("callsign") if ais else None,
            "type": _get_ship_type_name(ais.get("type")) if ais else None,
            "via_callsign": bool(ais and ais.get("via_callsign")),
            "evidence": str(ex.get("evidence") or "")[:200],
            "confidence": ex.get("confidence") if ex.get("confidence") in ("high", "medium", "low") else "low",
        })

    missing = sorted(valid_ids - seen)
    if missing:
        out.append({"chunk_ids": missing, "vessel": None, "mmsi": None, "callsign": None,
                    "type": None, "via_callsign": False,
                    "evidence": "not assigned by resolver", "confidence": "low"})
    return out


CONVERSATIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations.json")
CONVERSATIONS_KEEP = int(os.environ.get("CONVERSATIONS_KEEP", "300"))

_resolved: list[dict] = []
_resolved_lock = threading.Lock()


def _load_conversations() -> None:
    try:
        with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as fh:
            with _resolved_lock:
                _resolved[:] = json.load(fh)[-CONVERSATIONS_KEEP:]
        print(f"[conv] loaded {len(_resolved)} resolved exchanges", flush=True)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[conv] could not load {CONVERSATIONS_FILE}: {exc}", flush=True)


def _save_conversations() -> None:
    try:
        with _resolved_lock:
            data = list(_resolved[-CONVERSATIONS_KEEP:])
        with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
    except Exception as exc:
        print(f"[conv] could not save {CONVERSATIONS_FILE}: {exc}", flush=True)


def _store_resolved(window: list[dict], exchanges: list[dict]) -> None:
    """Record resolved exchanges together with the transmissions they cover, verbatim."""
    by_id = {c["id"]: c for c in window}
    rows = []
    for ex in exchanges:
        turns = [by_id[i] for i in ex["chunk_ids"] if i in by_id]
        if not turns:
            continue
        rows.append({
            **{k: v for k, v in ex.items() if k != "chunk_ids"},
            "channel": turns[0]["channel"],
            "start": turns[0]["time"].strftime("%Y-%m-%d %H:%M:%S"),
            "end":   turns[-1]["time"].strftime("%Y-%m-%d %H:%M:%S"),
            # Text is copied straight from the journal, never from the resolver.
            "turns": [{"time": t["time"].strftime("%H:%M:%S"),
                       "text": t.get("corrected") or t.get("text", ""),
                       "raw": t.get("text", ""),
                       "live_vessel": t.get("live_vessel")} for t in turns],
        })
    if not rows:
        return
    with _resolved_lock:
        _resolved.extend(rows)
        del _resolved[:-CONVERSATIONS_KEEP]
    _save_conversations()


def _resolve_window(window: list[dict]) -> None:
    exchanges = resolve_conversation(window)
    _store_resolved(window, exchanges)
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    for ex in exchanges:
        who = ex.get("vessel") or "unidentified"
        via = " via callsign" if ex.get("via_callsign") else ""
        print(f"[{ts}] [conv] {len(ex['chunk_ids'])} turns -> {who}{via} ({ex.get('confidence')})", flush=True)




def render_conversations_page(rows: list[dict]) -> str:
    """Render resolved exchanges, newest first. Built from stored data on every request."""
    blocks = []
    for row in reversed(rows):
        vessel = row.get("vessel")
        conf   = row.get("confidence", "low")
        ident  = _vessel_link(vessel, row.get("mmsi")) if vessel else "unidentified"
        badge  = "via callsign" if row.get("via_callsign") else f"{_html_escape(conf)} confidence"

        meta = []
        if row.get("mmsi"):
            meta.append(f"MMSI {_html_escape(row['mmsi'])}")
        if row.get("callsign"):
            meta.append(f"callsign {_html_escape(row['callsign'])}")
        if row.get("type"):
            meta.append(_html_escape(row["type"]))

        turns = []
        for t in row.get("turns", []):
            live = t.get("live_vessel")
            # Shown when the live guess disagreed, so the correction is visible rather than
            # silently overwritten.
            note = (f'<span class="was">live: {_html_escape(live)}</span>'
                    if live and live != vessel else "")
            turns.append(f'<li><span class="t">{_html_escape(t.get("time",""))}</span> '
                         f'{_html_escape(t.get("text",""))} {note}</li>')

        blocks.append(f"""
    <div class="conv {'named' if vessel else 'unnamed'}">
      <div class="hd">
        <span class="vessel">{ident}</span>
        <span class="badge {_html_escape(conf)}">{badge}</span>
        <span class="meta">{' &middot; '.join(meta)}</span>
        <span class="when">{_html_escape(row.get('start',''))} &ndash; {_html_escape(row.get('end',''))[-8:]}
              &middot; ch {_html_escape(row.get('channel',''))} &middot; {len(row.get('turns', []))} turns</span>
      </div>
      <div class="ev">{_html_escape(row.get('evidence',''))}</div>
      <ul>{''.join(turns)}</ul>
    </div>""")

    body = "".join(blocks) if blocks else '<p class="empty">No conversations resolved yet.</p>'
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Resolved Conversations</title>
<meta http-equiv="refresh" content="30">
<style>
 body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; color: #222; }}
 h1 {{ color: #333; }} a {{ color: #2c3e50; }}
 .conv {{ background: #fff; margin-bottom: 14px; padding: 12px 14px; border-radius: 4px;
          box-shadow: 0 1px 3px rgba(0,0,0,.12); border-left: 4px solid #bbb; }}
 .conv.named {{ border-left-color: #27ae60; }}
 .conv.unnamed {{ border-left-color: #e0b400; }}
 .hd {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: baseline; }}
 .vessel {{ font-weight: bold; font-size: 1.1em; }}
 .badge {{ font-size: .75em; padding: 2px 7px; border-radius: 10px; background: #eee; }}
 .badge.high {{ background: #d4edda; }} .badge.medium {{ background: #fff3cd; }}
 .badge.low {{ background: #f8d7da; }}
 .meta, .when {{ color: #666; font-size: .85em; }} .when {{ margin-left: auto; }}
 .ev {{ color: #555; font-style: italic; font-size: .9em; margin: 6px 0; }}
 ul {{ list-style: none; padding-left: 0; margin: 6px 0 0; }}
 li {{ padding: 3px 0; border-top: 1px solid #f0f0f0; font-size: .95em; }}
 .t {{ color: #888; font-family: monospace; margin-right: 8px; }}
 .was {{ color: #c0392b; font-size: .8em; margin-left: 6px; }}
 .empty {{ color: #666; }}
</style></head><body>
<h1>Resolved Conversations</h1>
<p><a href="/identified-vessels">Identified vessels log</a> &middot; {len(rows)} exchanges &middot; auto-refresh 30s</p>
<p style="color:#666;font-size:.9em">Identity is decided after each exchange ends, from the whole
exchange rather than one transmission. Transmission text is copied verbatim from the live
transcript &mdash; this pass never rewrites it.</p>
{body}
</body></html>"""


def _conversation_reaper() -> None:
    while True:
        threading.Event().wait(CONVERSATION_POLL_S)
        try:
            for window in _take_closed_windows():
                _resolve_window(window)
        except Exception as exc:
            print(f"  [conv reaper error] {exc}", flush=True)
