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

from stt_proxy.ais import (_find_ais_hints, _get_ship_type_name, _hint_probes, _km_from_maas,
                           match_by_callsign, match_by_callsign_pattern,
                           match_by_callsign_suffix, match_by_mmsi, match_by_name_candidates,
                           is_recent, suggest_vessels)
from stt_proxy.claude import _get_claude
from stt_proxy import conversation_correct
from stt_proxy.corrections import (_callsign_supported_by_text, _partial_callsign_pattern, _phonetic_callsign_probes,
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
            # The matched ship's last AIS fix, so the age of the match survives into the
            # stored turn. Same argument as live_mmsi beside it: without this the page cannot
            # tell a ship that was there from one that was days away, and it was calling both
            # "AIS-confirmed".
            "live_seen": result.get("ais_last_seen"),
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


# An age bound on the live-match path specifically.
#
# BELLONA, 2026-08-18: a 135x12 m inland barge drawing 1.5 m, bound for Antwerp, 72 km from
# Maas Center, whose last AIS fix was 122 HOURS old, was named with high confidence over
# GT VELA -- 12.8 km away and reporting seven minutes earlier. It reached the resolver
# entirely through this function: match_by_mmsi reads _mmsi_index directly, so AIS_MAX_AGE_MIN
# never applied here, and roughly half a real candidate list arrives by this route.
#
# Kept separate from AIS_MAX_AGE_MIN rather than folded into it: that setting bounds what can
# be FOUND by name across every matcher, and turning it on is a much larger change than
# declining to re-offer a vessel last heard from days ago.
#
# ON by default since 2026-08-18, on measurement. bench_identify --resolve --repeats 3 over
# the 08-13/14 labels, only this bound varied:
#
#                    off (0)   360 min
#     precision       87.1%     88.3%    +1.2
#     recall          65.6%     66.3%    +0.7
#     correct           386       386     unchanged
#     wrong              57        51     -6
#     missed            145       145     unchanged
#
# Spread 0.0 on every metric across three runs per arm, so this is signal and not the ~2.9
# points of resolver sampling noise. Nothing was lost: not one correct identification became
# a miss. All six transmissions that moved are a single conversation where nobody was
# identifiable and PRESTO -- last heard from 29 hours earlier -- was named across all six.
#
# This is a REMOVING-wrong-candidates change, and the contrast with adding is now measured
# from both ends: relaxing AIS_HINT_MIN_SCORE on 2026-08-12 added candidates and cost 11
# precision points; this removes them and gains 1.2.
#
# Six hours because a ship quiet through a couple of 900 s polls should still be offered,
# while yesterday's traffic should not. Only this value was measured -- there is no sweep
# behind it. ROLLBACK: AIS_LIVE_MATCH_MAX_AGE_MIN=0 restores the old behaviour exactly.
LIVE_MATCH_MAX_AGE_MIN = int(os.environ.get("AIS_LIVE_MATCH_MAX_AGE_MIN", "360"))


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
        if not is_recent(entry, LIVE_MATCH_MAX_AGE_MIN):
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

# The keyword-free phonetic-run anchor, separately switchable from the keyword-anchored
# pattern path above it. It exists as its own knob so the two can be A/B'd against each
# other with `bench_identify.py --resolve`: comparing the new code to verdicts STORED days
# ago measures every change since, not this one, and the prompt override fixed on 2026-08-07
# alone was worth 11 WER points. One variable at a time or the number means nothing.
AIS_PHONETIC_CALLSIGN           = os.environ.get("AIS_PHONETIC_CALLSIGN", "on").strip().lower() != "off"
PARTIAL_CALLSIGN_MIN_NAME_SCORE = int(os.environ.get("PARTIAL_CALLSIGN_MIN_NAME_SCORE", "60"))

# Trying the TAIL of a callsign that decoded cleanly but short.
#
# CLAMOR SCHULTE, 2026-08-18, callsign V7B2710: the decoder produced "7B2710", complete but
# for the leading V, which was swallowed before the spelling began ("call SUNvictor seven")
# rather than garbled within it. match_by_callsign_suffix resolves that run to exactly one
# cached vessel -- and was never asked, because it is reachable only through
# _partial_callsign_pattern, which declines a span containing no garble as the exact
# lookup's job. The exact lookup then cannot match a run that is a character short. Each
# path defers to the other; the ship goes unidentified with its callsign on the air.
#
# Both existing gates still apply: the tail must fit exactly one cached callsign, and a name
# resembling that vessel must be spoken somewhere in the window. Over the 300 stored
# conversations it fires on 4, agreeing with the stored verdict on 3 and supplying CLAMOR
# SCHULTE on the fourth -- no new wrong answers.
#
# ON by default since 2026-08-18, BY DECISION RATHER THAN BY MEASUREMENT. Recording that
# plainly because everything else here is the other way round. Its one bench arm read -0.9
# precision and was INVALID rather than negative: all four transmissions it moved were the
# ATLANTIC PRESTIGE conversation, whose label named a ship two vessels share, so the arm
# scored the fallback against a 2 m inland barge while the fallback had picked the 200 m
# ship that spelled out V7A6052 on air. The favourable evidence above is candidate
# inspection, which is a weaker instrument than an end-to-end arm and has misled here
# before -- relaxing AIS_HINT_MIN_SCORE also looked good by candidate recall and then cost
# 11 precision points.
#
# That arm IS re-runnable now: ambiguous labels stopped being scored in the same session, so
# the conversation that invalidated it is excluded. If identification regresses, this is the
# first thing to switch off. ROLLBACK: AIS_CALLSIGN_SUFFIX_FALLBACK=off.
CALLSIGN_SUFFIX_FALLBACK        = os.environ.get(
    "AIS_CALLSIGN_SUFFIX_FALLBACK", "on").strip().lower() == "on"


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
        text = chunk.get("text", "")

        # Keyword-anchored first: it bounds the span, so it is the more trustworthy of the
        # two and gets to claim the vessel before the keyword-free path is tried.
        probes: list[tuple[str, str]] = []
        decoded = _partial_callsign_pattern(text)
        if decoded:
            probes.append(("pattern", decoded[0]))
        if AIS_PHONETIC_CALLSIGN:
            probes += [("suffix", run) for run in _phonetic_callsign_probes(text)]
        if CALLSIGN_SUFFIX_FALLBACK:
            # A run that decoded CLEANLY but came out short. Nothing in it is garbled, so
            # _partial_callsign_pattern declines it as the exact lookup's job -- and the
            # exact lookup cannot match a callsign missing a character. Without this the
            # two paths hand it back and forth and the tail is never tried.
            probes += [("suffix", run) for run in _spelled_out_runs(text)
                       if len(run) >= CALLSIGN_RUN_MIN_LEN and not run.isdigit()
                       and not match_by_callsign(run)]

        for kind, probe in probes:
            entry = (match_by_callsign_pattern(probe) if kind == "pattern"
                     else match_by_callsign_suffix(probe))
            if not entry or not entry.get("mmsi"):
                continue
            # Both gates, always. A unique callsign tail with no name spoken anywhere in the
            # window is still a guess -- see the VISION/BERGE TOWNSEND conversation, where
            # the wrong ship scored exactly as well as the right one on name alone.
            if not _name_corroborates(entry["name"], chunks):
                continue
            marked = dict(entry)
            marked["via_partial_callsign"] = True
            marked["partial_pattern"] = probe
            found.setdefault(entry["mmsi"], marked)
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


# What the resolver was offered, recorded on every row it produces
#
# "not in the candidate list" is far and away the commonest reason a conversation resolves
# to nobody -- it is the stated reason in almost every unidentified row -- and until now the
# list itself was discarded the moment the call returned. That left no way to separate the
# two cases behind that sentence: a vessel that was never offered (a cache-membership
# problem) from one that was offered and rejected (a resolver-judgement problem). They need
# opposite fixes, and both BORIS SOKOLOV and SEA BANCKERT were undiagnosable without it.
#
# Name, MMSI, how each candidate got on the list, and the four facts that judge whether it
# was PLAUSIBLE -- where it was, how deep it sat, where it was going, and when it was last
# heard from. Not every AIS field: imo, sog and cog judge nothing here and this is written
# three hundred times over.
#
# Position and the rest were left out at first, on the argument that they answered no
# question this record existed to answer. Two measurements then failed for want of exactly
# them. A vessel's position is only knowable at the moment it is used: a frozen cache keeps
# only each ship's LATEST fix, so NOORDBORG reads as 101.6 km away in a snapshot taken a day
# after it called, and any retrospective proximity question scores where the ship ended up
# rather than where it was on the radio. And BELLONA -- the misidentification that prompted
# all of this -- was recognisable as wrong precisely by draught 1.5 m and destination
# ANTWERPEN on a Rotterdam approach channel, plus a fix 122 hours old.
#
# Not recording these does not defer those questions. It destroys them.
_CANDIDATE_MARKS = ("via_callsign", "via_live_match", "via_partial_callsign")
_CANDIDATE_FACTS = ("latitude", "longitude", "draught", "destination", "last_seen")


def _record_candidates(rows: list[dict], candidates: list[dict]) -> list[dict]:
    compact = [{"name": c.get("name"), "mmsi": c.get("mmsi"),
                **{mark: bool(c.get(mark)) for mark in _CANDIDATE_MARKS},
                **{fact: c.get(fact) for fact in _CANDIDATE_FACTS}}
               for c in candidates]
    for row in rows:
        row["resolver_candidates"] = compact
    return rows


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
            # Sampling default is 1.0, and this call had been leaving it there while
            # identify.py:140 has pinned temperature=0 all along. That made the resolver
            # non-repeatable, which matters far more here than a point of accuracy: every
            # A/B run with `bench_identify.py --resolve` was measuring the change plus the
            # sampling noise, with no way to tell them apart. Two runs on 2026-08-08 named
            # different off-list vessels ('NORDIC SAGA' vs 'ST NIKOLAI') from identical
            # inputs, which is exactly that noise made visible. Adjudicating identity is a
            # judgement over fixed evidence -- there is nothing here that wants sampling.
            temperature=0,
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
        # Recorded on this path too: "the resolver never ran" and "the resolver ran and
        # found nobody" look identical in the store otherwise, and only one of them is a
        # reason to go looking at the candidate list.
        return _record_candidates(_unresolved(chunks), candidates)

    return _record_candidates(_validate_exchanges(exchanges, chunks, by_name), candidates)


def _unresolved(chunks: list[dict]) -> list[dict]:
    """Fallback: one exchange, nobody identified. Never loses a transmission."""
    return [{"chunk_ids": [c["id"] for c in chunks], "vessel": None, "mmsi": None,
             "evidence": "resolver unavailable", "confidence": "low"}]


# What the AIS match knows about the ship beyond its name, kept in one place so the snapshot
# below and the renderer cannot drift apart.
_PARTICULARS = ("imo", "length", "beam", "draught", "destination",
                "latitude", "longitude", "sog", "cog", "heading")


def _format_particulars(row: dict) -> str:
    """The ship's particulars as display bits, omitting whatever AIS did not supply.

    A vessel matched by callsign alone carries dimensions but no position, so each bit is
    included only when its values are present -- a row of dashes says nothing.
    """
    bits = []
    if row.get("imo"):
        bits.append(f"IMO {_html_escape(row['imo'])}")
    if row.get("length") and row.get("beam"):
        bits.append(f"{int(row['length'])} &times; {int(row['beam'])} m")
    elif row.get("length"):
        bits.append(f"{int(row['length'])} m")
    if row.get("draught") is not None:
        bits.append(f"draught {row['draught']:.1f} m")
    # Free text off the radio, so the most attacker-controllable field on the feed: anyone
    # with a transmitter in the Rotterdam box can set it to whatever they like.
    if row.get("destination"):
        bits.append(f"&rarr; {_html_escape(row['destination'])}")
    if row.get("sog") is not None:
        bits.append(f"{row['sog']:.1f} kn")
    if row.get("cog") is not None:
        bits.append(f"{int(row['cog'])}&deg;")
    if row.get("latitude") is not None and row.get("longitude") is not None:
        bits.append(f"{row['latitude']:.4f}, {row['longitude']:.4f}")
    return " &middot; ".join(bits)


def _format_candidates(row: dict) -> str:
    """The candidate list for a contested identification, or "" when there is nothing to choose.

    Rendered only for two or more: a single candidate is an answer, and presenting it as a
    choice would train the reader to ignore the block that matters.

    This is a display, not a feedback loop -- clicking records nothing. Deliberate: a click
    that recorded "this was the right ship" is free labelled ground truth for the bench, but
    it needs a store, a schema and a correction path, none of which this needs.
    """
    candidates = row.get("candidates") or []
    if len(candidates) < 2:
        return ""

    items = []
    for c in candidates:
        bits = []
        if c.get("type"):
            bits.append(_html_escape(c["type"]))
        if c.get("km") is not None:
            bits.append(f"{float(c['km']):.1f} km from Maas Center")
        if c.get("destination"):
            bits.append(f"dest {_html_escape(c['destination'])}")
        if c.get("last_seen"):
            bits.append(f"seen {_html_escape(c['last_seen'])}")
        items.append(
            f'<li>{_vessel_link(c.get("name", "?"), c.get("mmsi"))} '
            f'<span class="cmeta">{" &middot; ".join(bits)}</span></li>')

    return (f'<div class="cands"><span class="clabel">{len(candidates)} candidates '
            f'&mdash; pick the one that fits what was said:</span>'
            f'<ul>{"".join(items)}</ul></div>')


# ---------------------------------------------------------------------------
# Suggestions for a conversation nobody was identified in
# ---------------------------------------------------------------------------
#
# When the resolver names nobody, the page offers the closest names anyway, below the
# cutoff, for the reader to judge by ear. Measured on the 08-13/14 labels: of the 35
# conversations left unidentified, a three-item shortlist holds the right ship 9 times.
#
# Display only. Nothing here reaches `vessel`, `mmsi`, the vessel log, the resolver's
# candidate list or the conversation-correction pass -- the shortlist is computed after the
# identity is already final, and is never read back. That separation is the whole safety
# argument: it is why this cannot repeat THULELAND, where a sub-cutoff match rewrote
# "motor vessel to Leland" into "motor vessel Vlieland" and named the wrong ship.
SUGGEST           = os.environ.get("AIS_SUGGEST", "on").strip().lower() != "off"
SUGGEST_N         = int(os.environ.get("AIS_SUGGEST_N", "3"))
SUGGEST_DF_MAX    = float(os.environ.get("AIS_SUGGEST_DF_MAX", "0.05"))
# Below this many stored conversations the frequency table cannot tell a ship from the
# station, so the shortlist would be MAAS, MAS, MAAS on every row. Show nothing instead.
SUGGEST_MIN_DOCS  = int(os.environ.get("AIS_SUGGEST_MIN_DOCS", "30"))


def _boilerplate_filter(rows: list[dict]):
    """A predicate: is this word span specific enough to be worth looking up as a name?

    Vessel names are rare -- a ship calls once or twice and is gone. Procedure is not: on
    this channel "MAAS" appears in 93% of stored conversations and "MAAS APPROACH" in 91%,
    because that is how every call opens. Two real cargo ships carry those names, and they
    took 56 of the 105 top-three shortlist slots before this filter existed.

    Document frequency rather than a hand-written place list, so it re-learns whatever
    station is on air -- point the receiver at the Aviation band and it adapts by itself.
    """
    counts: dict[str, int] = {}
    docs = 0
    for row in rows:
        # The page shows corrected text where there is any, so count the same words the
        # reader sees -- otherwise a probe can be boilerplate on screen and rare in here.
        text = " ".join((t.get("conv") or t.get("text") or "") for t in row.get("turns") or [])
        if not text.strip():
            continue
        docs += 1
        for probe in set(_hint_probes(text)):
            counts[probe] = counts.get(probe, 0) + 1
    if not docs:
        return lambda probe: True
    return lambda probe: counts.get(probe, 0) / docs <= SUGGEST_DF_MAX


def _attach_suggestions(row: dict) -> None:
    """Add `row["suggestions"]`: the closest names, whether or not anyone was named.

    Until 2026-08-20 this returned early on an identified row, reasoning that a named
    conversation is answered and a shortlist beside it invites second-guessing an
    identification carrying evidence the shortlist lacks.

    LISTA/LISCA NERA M disproved the premise. The resolver named LISTA -- three days stale --
    because the single probe "LIST" scored 88.9. The ship actually calling, LISCA NERA M, had
    been seen five minutes earlier and scored 78.3 on "LIST CANERA", below the cutoff of 85.
    The identification carried no evidence the shortlist lacked; it was one short probe
    against a stale name, and the shortlist holding the right answer was suppressed precisely
    BECAUSE that wrong answer was confident. A wrong confident answer is when the near misses
    are worth most.

    Mutates in place and leaves `vessel` and `mmsi` alone -- see the note above. Nothing here
    is ever asserted, so the precision the cutoff protects is untouched by construction.
    Absent entirely when there is nothing to offer: an empty block would train the reader to
    skip the one that matters.
    """
    if not SUGGEST:
        return
    with _resolved_lock:
        corpus = list(_resolved)
    if len(corpus) < SUGGEST_MIN_DOCS:
        return
    text = " ".join((t.get("conv") or t.get("text") or "") for t in row.get("turns") or [])
    named = str(row.get("mmsi") or "")
    # One more than needed when a ship was named, so dropping it below does not leave the
    # shortlist a name short.
    found = suggest_vessels(text, probe_filter=_boilerplate_filter(corpus),
                            n=SUGGEST_N + 1 if named else SUGGEST_N)
    if named:
        # The answer does not belong in its own list of alternatives: a slot spent restating
        # it is a slot not spent on the ship that might be right instead.
        found = [s for s in found if str(s.get("mmsi") or "") != named][:SUGGEST_N]
    if found:
        row["suggestions"] = found


def _format_suggestions(row: dict) -> str:
    """The shortlist of closest names, or "" when there is none.

    The remark is not a caption -- it is what separates this block from an identification.
    Every row stored before this feature existed simply lacks the key.

    Two remarks, because the block answers two different questions. With nobody named it is
    "nothing cleared the bar; here is what came closest". With a ship named it is "here is what
    else was close" -- which is the LISTA/LISCA NERA M case, where the named ship was wrong and
    the right one sat just under the cutoff. Printing the unidentified wording beside a name
    would be a plain falsehood.
    """
    suggestions = row.get("suggestions") or []
    if not suggestions:
        return ""
    if row.get("vessel"):
        return ('<div class="suggest"><span class="slabel">Others that came close &mdash; these '
                'scored <em>below the identification cutoff</em>, so they were not considered. '
                'Check them if the name above looks wrong:'
                f'</span><ol>{"".join(_suggestion_items(suggestions))}</ol></div>')

    return ('<div class="suggest"><span class="slabel">Possible matches &mdash; these scored '
            '<em>below the identification cutoff</em>, so nobody was named. Unconfirmed:'
            f'</span><ol>{"".join(_suggestion_items(suggestions))}</ol></div>')


def _suggestion_items(suggestions: list[dict]) -> list[str]:
    """One <li> per suggestion. Shared by both remarks above so the rows cannot diverge."""
    return [
        f'<li><span class="srank">{i}</span>'
        f'{_vessel_link(s.get("name", "?"), s.get("mmsi"))} '
        f'<span class="sscore">{float(s.get("score", 0)):.0f}</span> '
        f'<span class="sheard">heard &ldquo;{_html_escape(str(s.get("heard", "")).title())}'
        f'&rdquo;</span></li>'
        for i, s in enumerate(suggestions, 1)]


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
        row = {
            "chunk_ids": sorted(ids),
            "vessel": ais["name"] if ais else None,
            "mmsi": ais.get("mmsi") if ais else None,
            "callsign": ais.get("callsign") if ais else None,
            "type": _get_ship_type_name(ais.get("type")) if ais else None,
            # The raw code as well as the name. The name is the category -- a tanker carrying
            # hazardous category B is just "Tanker" -- and the code is the only thing that can
            # still say which. The control panel reads it for the type tooltip. Rows stored
            # before this existed simply lack the key, and fall back to the name alone.
            "type_code": ais.get("type") if ais else None,
            "via_callsign": bool(ais and ais.get("via_callsign")),
            "evidence": str(ex.get("evidence") or "")[:200],
            "confidence": ex.get("confidence") if ex.get("confidence") in ("high", "medium", "low") else "low",
        }
        # Snapshotted, not looked up when the page renders: position, speed and course are
        # live values, so drawing an hours-old exchange against the ship's current position
        # would place it somewhere it was not when it called. The static fields come along
        # for the ride rather than being fetched separately.
        row.update({field: (ais.get(field) if ais else None) for field in _PARTICULARS})

        # Attached to the exchange so _store_resolved's `**ex` spread carries it through to
        # the page with no schema change. Rows stored before this existed simply lack the key.
        if row["vessel"]:
            found = match_by_name_candidates(row["vessel"])
            if len(found) > 1:
                row["candidates"] = [{
                    "name": c.get("name"),
                    "mmsi": c.get("mmsi"),
                    "type": _get_ship_type_name(c.get("type")),
                    "type_code": c.get("type"),
                    "km": (_km_from_maas(c["latitude"], c["longitude"])
                           if c.get("latitude") is not None
                           and c.get("longitude") is not None else None),
                    "destination": c.get("destination"),
                    "last_seen": c.get("last_seen"),
                } for c in found]
        out.append(row)

    missing = sorted(valid_ids - seen)
    if missing:
        out.append({"chunk_ids": missing, "vessel": None, "mmsi": None, "callsign": None,
                    "type": None, "via_callsign": False,
                    "evidence": "not assigned by resolver", "confidence": "low"})
    return out


_DEFAULT_CONVERSATIONS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "conversations.json")
# Overridable like AIS_CACHE_FILE, so a test or a second deployment can point the store
# somewhere private instead of writing beside the code.
CONVERSATIONS_FILE = os.path.normpath(
    os.environ.get("CONVERSATIONS_FILE", "").strip() or _DEFAULT_CONVERSATIONS_FILE)
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


def _store_resolved(window: list[dict], exchanges: list[dict],
                    corrections: dict[int, dict] | None = None) -> None:
    """Record resolved exchanges together with the transmissions they cover, verbatim.

    `corrections` maps chunk id to the conversation pass's output. It is stored ALONGSIDE the
    verbatim text, never over it: a reader must always be able to recover what was heard.
    """
    corrections = corrections or {}
    by_id = {c["id"]: c for c in window}
    rows = []
    for ex in exchanges:
        turns = [by_id[i] for i in ex["chunk_ids"] if i in by_id]
        if not turns:
            continue
        stored_turns = []
        for t in turns:
            row = {"time": t["time"].strftime("%H:%M:%S"),
                   "text": t.get("corrected") or t.get("text", ""),
                   "raw": t.get("text", ""),
                   "live_vessel": t.get("live_vessel"),
                   # Stored beside the name because the name alone is ambiguous:
                   # enrich_with_ais returns the result untouched when AIS matches nothing,
                   # so live_vessel can mean either "AIS matched this ship" or "the model
                   # heard this name and AIS had no such ship". Those have opposite causes
                   # -- a matcher problem versus a cache-membership problem -- and only the
                   # MMSI separates them. Its absence blocked the BORIS SOKOLOV diagnosis
                   # on 2026-08-13 and the same question again five days later.
                   "live_mmsi": t.get("live_mmsi"),
                   # And when that ship was last seen, so "AIS-confirmed" has to be earned
                   # rather than assumed. Turns stored before 2026-08-20 lack it and are
                   # shown as a match of unknown age, never as a confirmation.
                   "live_seen": t.get("live_seen")}
            fix = corrections.get(t["id"])
            # Absent rather than equal-to-text when nothing was corrected, so the page can
            # tell "not corrected" from "corrected to the same thing".
            if fix and fix.get("changes"):
                row["conv"] = fix["text"]
                row["changes"] = fix["changes"]
            stored_turns.append(row)
        rows.append({
            **{k: v for k, v in ex.items() if k != "chunk_ids"},
            "channel": turns[0]["channel"],
            "start": turns[0]["time"].strftime("%Y-%m-%d %H:%M:%S"),
            "end":   turns[-1]["time"].strftime("%Y-%m-%d %H:%M:%S"),
            "turns": stored_turns,
        })
    if not rows:
        return
    # Before the rows join the corpus, so a conversation cannot vote on what counts as
    # boilerplate in its own shortlist.
    for row in rows:
        _attach_suggestions(row)
    with _resolved_lock:
        _resolved.extend(rows)
        del _resolved[:-CONVERSATIONS_KEEP]
    _save_conversations()


def _resolve_window(window: list[dict]) -> None:
    exchanges = resolve_conversation(window)

    # Correction runs per EXCHANGE, not per window: a window can hold several unrelated
    # exchanges, and letting one conversation's context edit another's turns is the failure
    # this split exists to prevent.
    corrections: dict[int, dict] = {}
    if conversation_correct.CONVERSATION_CORRECT:
        by_id = {c["id"]: c for c in window}
        for ex in exchanges:
            turns = [by_id[i] for i in ex["chunk_ids"] if i in by_id]
            fixed = conversation_correct.correct_conversation(turns, ex.get("vessel"))
            if fixed:
                corrections.update(fixed)

    _store_resolved(window, exchanges, corrections)
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    for ex in exchanges:
        who = ex.get("vessel") or "unidentified"
        via = " via callsign" if ex.get("via_callsign") else ""
        print(f"[{ts}] [conv] {len(ex['chunk_ids'])} turns -> {who}{via} ({ex.get('confidence')})", flush=True)




def render_conversations_page(rows: list[dict]) -> str:
    """Render resolved exchanges, newest first. Built from stored data on every request."""
    blocks = []
    any_corrections = False
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
        corrected_count = 0
        for t in row.get("turns", []):
            live = t.get("live_vessel")
            # Shown when the live guess disagreed, so the correction is visible rather than
            # silently overwritten.
            note = (f'<span class="was">live: {_html_escape(live)}</span>'
                    if live and live != vessel else "")

            shown = t.get("conv") or t.get("text", "")
            changes = t.get("changes") or []
            if t.get("conv"):
                corrected_count += 1
                # title= rather than a second line: the original stays one hover away without
                # doubling the length of every conversation on the page.
                detail = "; ".join(
                    f'{c.get("from","")} -> {c.get("to","")} ({c.get("reason","")})'
                    for c in changes)
                body = (f'<span class="fixed" title="was: {_html_escape(t.get("text",""))}'
                        f' &#10;{_html_escape(detail)}">{_html_escape(shown)}</span>')
            else:
                body = _html_escape(shown)

            turns.append(f'<li><span class="t">{_html_escape(t.get("time",""))}</span> '
                         f'{body} {note}</li>')

        if corrected_count:
            any_corrections = True
        fixed_badge = (f'<span class="badge fixedcount">{corrected_count} corrected</span>'
                       if corrected_count else "")

        # Omitted entirely rather than rendered empty: conversations that resolved to nobody,
        # and the rows stored before these fields existed, have nothing to say here.
        particulars = _format_particulars(row)
        ais_line = f'\n      <div class="ais">{particulars}</div>' if particulars else ""
        cand_block = _format_candidates(row) + _format_suggestions(row)

        blocks.append(f"""
    <div class="conv {'named' if vessel else 'unnamed'}">
      <div class="hd">
        <span class="vessel">{ident}</span>
        <span class="badge {_html_escape(conf)}">{badge}</span>{fixed_badge}
        <span class="meta">{' &middot; '.join(meta)}</span>
        <span class="when">{_html_escape(row.get('start',''))} &ndash; {_html_escape(row.get('end',''))[-8:]}
              &middot; ch {_html_escape(row.get('channel',''))} &middot; {len(row.get('turns', []))} turns</span>
      </div>{ais_line}{cand_block}
      <div class="ev">{_html_escape(row.get('evidence',''))}</div>
      <ul>{''.join(turns)}</ul>
    </div>""")

    body = "".join(blocks) if blocks else '<p class="empty">No conversations resolved yet.</p>'
    # Chosen from what the page is actually showing, not from the flag: CONVERSATION_CORRECT
    # can be off, or on but yet to correct anything on this page, and either way a rendered
    # row with no "conv" field means the promise that this pass rewrites text would be false.
    correction_note = (
        "Text marked with a dotted underline was corrected using\n"
        "the rest of the conversation &mdash; hover it to see what was heard and why it changed."
        if any_corrections else
        "Transmission text is copied verbatim from the live\n"
        "transcript &mdash; this pass never rewrites it."
    )
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
 .ais {{ color: #2c3e50; font-size: .85em; margin-top: 5px; font-family: monospace; }}
 .ev {{ color: #555; font-style: italic; font-size: .9em; margin: 6px 0; }}
 ul {{ list-style: none; padding-left: 0; margin: 6px 0 0; }}
 li {{ padding: 3px 0; border-top: 1px solid #f0f0f0; font-size: .95em; }}
 .t {{ color: #888; font-family: monospace; margin-right: 8px; }}
 .was {{ color: #c0392b; font-size: .8em; margin-left: 6px; }}
 .fixed {{ border-bottom: 1px dotted #2c7; cursor: help; }}
 .badge.fixedcount {{ background: #d4edda; }}
 .empty {{ color: #666; }}
 .cands{{margin:.4em 0 .2em 0;padding:.4em .6em;border-left:3px solid #b58900;background:#fbf6e6}}
 .cands .clabel{{font-size:.85em;color:#8a6d00}}
 .cands ul{{margin:.3em 0 0 0;padding-left:1.2em}}
 .cands .cmeta{{color:#666;font-size:.85em}}
 .suggest{{margin:.4em 0 .2em 0;padding:.4em .6em;border-left:3px solid #7f8c8d;background:#f0f2f3}}
 .suggest .slabel{{font-size:.85em;color:#555}} .suggest .slabel em{{color:#c0392b;font-style:normal}}
 .suggest ol{{margin:.3em 0 0 0;padding-left:0;list-style:none}}
 .suggest li{{padding:1px 0}}
 .srank{{color:#888;font-family:monospace;font-size:.85em;margin-right:.5em}}
 .sscore{{color:#666;font-family:monospace;font-size:.8em;border:1px solid #ccc;
          border-radius:2px;padding:0 .3em}}
 .sheard{{color:#666;font-size:.85em;font-style:italic}}
</style></head><body>
<h1>Resolved Conversations</h1>
<p><a href="/identified-vessels">Identified vessels log</a> &middot; {len(rows)} exchanges &middot; auto-refresh 30s</p>
<p style="color:#666;font-size:.9em">Identity is decided after each exchange ends, from the whole
exchange rather than one transmission. {correction_note}</p>
{body}
</body></html>"""


def _reap_pass() -> None:
    """Take whatever windows have closed and resolve each independently.

    Isolated per window on purpose: _take_closed_windows has already removed a closed
    window's chunks from the journal by the time _resolve_window runs, so a single bad
    reply (or any other surprise) inside one window must not cost the rest of the batch --
    they would otherwise be lost permanently, never stored, never rendered.
    """
    try:
        windows = _take_closed_windows()
    except Exception as exc:
        print(f"  [conv reaper error] {exc}", flush=True)
        return
    for window in windows:
        try:
            _resolve_window(window)
        except Exception as exc:
            print(f"  [conv reaper error] {exc}", flush=True)


def _conversation_reaper() -> None:
    while True:
        threading.Event().wait(CONVERSATION_POLL_S)
        _reap_pass()
