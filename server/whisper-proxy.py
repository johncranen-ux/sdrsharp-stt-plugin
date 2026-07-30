"""
Whisper STT proxy — listens on :9000, transcribes audio via the selected STT backend
(Groq's hosted Whisper API, or a local whisper.cpp server on :8080 in WSL2),
post-processes maritime transcriptions with Claude to extract vessel names and callsigns,
fuzzy-matches against a live AIS feed (aisstream.io) for Rotterdam / Maas Approach,
and returns enriched text to the SDRSharp plugin.

Usage:
    py whisper-proxy.py

Required env vars:
    ANTHROPIC_API_KEY   — Claude API key (for maritime vessel extraction)
    AISSTREAM_API_KEY   — aisstream.io API key (free at aisstream.io)
    GROQ_API_KEY        — Groq API key (only when STT_BACKEND=groq, the default)

Optional env vars:
    STT_BACKEND           — "groq" (default) or "whisper_cpp"
    GROQ_MODEL            — Groq model id (default: whisper-large-v3)
    GROQ_TIMEOUT_S        — Groq HTTP timeout (default: 30, plugin cancels at 60)
    GROQ_QUOTA_WARN_AT    — warn below this many daily requests left (default: 200)
    GROQ_QUOTA_WARN_STEP  — repeat the warning every N further requests (default: 50)
    WHISPER_BACKEND_PORT  — override local backend port (default: 8080)
    PROXY_PORT            — override proxy listen port (default: 9000)

Switching backends is deliberately a config change, not a code change: set
STT_BACKEND=whisper_cpp in start-all.bat and restart the proxy to fall back to
the local GPU. Both paths are fully maintained.
"""

import http.server
import http.client
import json
import os

# This Python install's default OpenSSL cert path (C:\Program Files\Common Files\SSL\cert.pem)
# doesn't exist on Windows, so outbound HTTPS/WSS (Claude API, aisstream.io) fail SSL
# verification unless pointed at certifi's bundle explicitly. Must run before anthropic/
# websockets create their first SSL context, so it's set here before those imports.
if not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass

import atexit
import datetime
import re
import subprocess
import time
from rapidfuzz import process as rf_process, fuzz as rf_fuzz
import threading
import anthropic


PROXY_PORT   = int(os.environ.get("PROXY_PORT", "9000"))
BACKEND_HOST = "localhost"
BACKEND_PORT = int(os.environ.get("WHISPER_BACKEND_PORT", "8080"))

# Which STT backend transcribes audio. "groq" (default) calls Groq's hosted Whisper
# API; "whisper_cpp" calls the local whisper.cpp server in WSL2. The local GPU (RX
# 7900 XTX) has a hardware fault that hangs the ROCm driver mid-inference and costs
# one chunk of radio audio per event -- see docs/rocm-upgrade-runbook.md -- so the
# cloud is the default. The local path is kept fully working as the fallback.
STT_BACKEND = os.environ.get("STT_BACKEND", "groq").strip().lower()

GROQ_HOST      = "api.groq.com"
GROQ_PATH      = "/openai/v1/audio/transcriptions"
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL     = os.environ.get("GROQ_MODEL", "whisper-large-v3").strip()
GROQ_TIMEOUT_S = float(os.environ.get("GROQ_TIMEOUT_S", "30"))

# Groq documents a 224-token cap on `prompt`. Tokens aren't countable here without
# pulling in a tokenizer, so cap on words with enough slack that even a worst-case
# ~1.5 tokens/word prompt stays inside the limit.
GROQ_PROMPT_MAX_WORDS = int(os.environ.get("GROQ_PROMPT_MAX_WORDS", "140"))

# How long a 429 may ask us to wait before we give up and let the chunk fail. The
# plugin's send loop is serial, so sleeping here stalls every chunk behind this one.
GROQ_MAX_RETRY_WAIT_S = float(os.environ.get("GROQ_MAX_RETRY_WAIT_S", "5"))

# Warn once the daily request allowance drops below GROQ_QUOTA_WARN_AT, then again
# on every GROQ_QUOTA_WARN_STEP consumed after that.
GROQ_QUOTA_WARN_AT   = int(os.environ.get("GROQ_QUOTA_WARN_AT", "200"))
GROQ_QUOTA_WARN_STEP = int(os.environ.get("GROQ_QUOTA_WARN_STEP", "50"))

# Rotterdam / Maas Approach bounding box  [SW corner, NE corner]
ROTTERDAM_BBOX = [[[51.0, 2.95], [52.85, 6.0]]]

AIS_CACHE_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ais_cache.json")
VESSELS_LOG_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "identified_vessels.html")
AIS_SAVE_INTERVAL   = 300

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
# AIS ship type mappings
# ---------------------------------------------------------------------------
# AIS vessel data and name matching -- see stt_proxy/ais.py
#
# The module is imported as a whole, not just its names: the vessel caches are mutated by
# the feed thread, so they must be read through `ais.` to see current state. Tests patch
# them there for the same reason.
# ---------------------------------------------------------------------------

from stt_proxy import ais  # noqa: E402
from stt_proxy.ais import (  # noqa: E402
    AIS_CACHE_FILE,
    AIS_HINT_FILTER,
    AIS_HINT_MIN_SCORE,
    AIS_SAVE_INTERVAL,
    AIS_SHIP_TYPES,
    ROTTERDAM_BBOX,
    _ais_thread,
    _cache_size,
    _find_ais_hints,
    _get_ship_type_name,
    _hint_probes,
    _load_cache,
    _periodic_save,
    _save_cache,
    match_by_callsign,
    match_by_name,
)




def _init_vessels_log() -> None:
    if os.path.exists(VESSELS_LOG_FILE):
        return
    html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Identified Vessels</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        h1 { color: #333; }
        table { border-collapse: collapse; width: 100%; background: white; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        th { background: #2c3e50; color: white; padding: 12px; text-align: left; }
        td { padding: 10px 12px; border-bottom: 1px solid #ddd; }
        tr:hover { background: #f9f9f9; }
        .match { background: #d4edda; }
        .no-match { background: #fff3cd; }
    </style>
</head>
<body>
    <h1>Identified Vessels Log</h1>
    <table>
        <thead>
            <tr>
                <th>Timestamp</th><th>Vessel</th><th>MMSI</th><th>Callsign</th>
                <th>Type</th><th>AIS Type</th><th>IMO</th><th>Length</th>
                <th>Lat</th><th>Lon</th><th>Speed</th><th>Course</th><th>Transcription</th>
            </tr>
        </thead>
        <tbody id="vessels">
        </tbody>
    </table>
    <script>setInterval(() => location.reload(), 5000);</script>
</body>
</html>
"""
    try:
        with open(VESSELS_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as exc:
        print(f"[Vessels Log] init error: {exc}", flush=True)


_log_lock = threading.Lock()


def _append_vessel_to_log(result: dict, raw_text: str) -> None:
    try:
        vessel   = result.get("vessel") or "-"
        mmsi     = result.get("mmsi") or "-"
        callsign = result.get("callsign") or "-"
        vtype    = result.get("vessel_type") or "-"
        ais_type = _get_ship_type_name(result.get("type")) or "-"
        imo      = result.get("imo") or "-"
        length   = f"{result.get('length')}m" if result.get("length") else "-"
        lat      = f"{result.get('latitude'):.4f}" if result.get("latitude") is not None else "-"
        lon      = f"{result.get('longitude'):.4f}" if result.get("longitude") is not None else "-"
        speed    = f"{result.get('sog'):.1f}" if result.get("sog") is not None else "-"
        course   = f"{int(result.get('cog'))}" if result.get("cog") is not None else "-"
        ts       = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row_class = "match" if result.get("mmsi") else "no-match"
        preview   = raw_text[:80] + ("..." if len(raw_text) > 80 else "")

        row = f"""        <tr class="{row_class}">
            <td>{ts}</td><td><strong>{vessel}</strong></td><td>{mmsi}</td><td>{callsign}</td>
            <td>{vtype}</td><td>{ais_type}</td><td>{imo}</td><td>{length}</td>
            <td>{lat}</td><td>{lon}</td><td>{speed}</td><td>{course}°</td>
            <td><em>{preview}</em></td>
        </tr>
"""
        with _log_lock:
            with open(VESSELS_LOG_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            content = content.replace('        <tbody id="vessels">', '        <tbody id="vessels">\n' + row)
            with open(VESSELS_LOG_FILE, "w", encoding="utf-8") as f:
                f.write(content)
    except Exception as exc:
        print(f"[Vessels Log] append error: {exc}", flush=True)




# ---------------------------------------------------------------------------
# Text filtering and correction -- see stt_proxy/corrections.py
#
# Imported by name so existing call sites read unchanged. Note that the module-level
# switches (PROMPT_ECHO_FILTER, MAAS_FUZZ_THRESHOLD) are read inside that module, so
# tests must patch them there rather than here.
# ---------------------------------------------------------------------------

from stt_proxy.corrections import (  # noqa: E402
    MAAS_FUZZ_THRESHOLD,
    PROMPT_ECHO_FILTER,
    PROMPT_ECHO_MIN_WORDS,
    _apply_sttt_corrections,
    _callsign_supported_by_text,
    _correct_maas_before_approach,
    _is_hallucination,
    _is_prompt_echo,
    _prompt_echo_tokens,
    _spelled_out_runs,
)


_claude = None


def _get_claude() -> anthropic.Anthropic:
    global _claude
    if _claude is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        # Bounded worst case: this call sits in the middle of the live transcription path
        # (blocks the CH01 response until it returns), so cap it well below the client's
        # own patience rather than trusting the SDK's much longer default.
        _claude = anthropic.Anthropic(api_key=api_key, timeout=15.0, max_retries=1)
    return _claude


# Callsign verification
#
# The extractor used to be told to "always extract callsigns", and it obliged even when the
# transmission contained none: "Gungor Star one three one five, correct." produced VRSQ4,


SYSTEM_PROMPT = """\
You analyse VHF marine radio transcriptions from Rotterdam harbour (Maas Approach / Rotterdam VTS area).
Correct fuzzy STT errors using maritime context. Return ONLY raw JSON, no markdown:
{"vessel": "<name or null>", "callsign": "<callsign or null>", "vessel_type": "<type or null>", "text": "<corrected text>"}

Rules:
1. Shore stations (Maas Approach, Rotterdam VTS, Pilot) are NOT vessels.
2. Extract vessel names: after "this is", "calling", vessel type words, or when shore station addresses a vessel.
3. Extract a callsign ONLY when the transmission spells one out -- phonetically
   ("Juliet Lima Sierra Romeo"), as characters ("9 Hotel Alpha six one"), or verbatim
   ("9HF5093"). If no callsign was spoken, return null. Do not guess one from the vessel
   name, from the AIS hints, or from anything else: a callsign nobody said is worse than
   no callsign, because it looks up to a real ship.
4. Correct STT errors: mass->maas, draft->draught, boys->buoys, motor tanker->Motortanker.
5. vessel_type: tanker/bulker/container/tug/ferry/general_cargo/passenger/yacht/pilot/null.
6. [AIS: ...] hints are nearby vessels, NOT a list of who is speaking. Only use a hint to
   fix the spelling of a name the speaker actually said. Never take a vessel name from the
   hints alone: if the transmission does not name a vessel, return null even when hints
   are present. "Yes, good day sir" names no vessel, whatever the hints say.
7. "text" is a transcription of THIS transmission and nothing else. Fix mis-heard words,
   but never add content: no vessel name that was not spoken here, no completing of a
   half-finished sentence. If the whole transmission was "Maas Approach." then "text" is
   "Maas Approach." -- NOT "Maas Approach, <vessel>." Identifying the speaker is what the
   "vessel" field is for.
"""


def extract_vessel(raw_text: str, channel: str = "",
                   now: datetime.datetime | None = None) -> dict:
    """Identify the vessel in a single transmission, live.

    Deliberately sees only this transmission: conversation context in this call bleeds into
    the transcription it also produces. Cross-turn identity is settled afterwards by
    resolve_conversation(), which cannot touch the text because its schema has no text field.
    """
    hints = _find_ais_hints(raw_text)
    blocks = [raw_text]

    if hints:
        hint_parts = []
        for h in hints:
            parts = [f"{h['name']} (MMSI:{h['mmsi']})"]
            if h.get("callsign"):
                parts.append(f"cs:{h['callsign']}")
            if h.get("type"):
                parts.append(f"type:{_get_ship_type_name(h['type'])}")
            hint_parts.append(" ".join(parts))
        blocks.append(f"[AIS: {', '.join(hint_parts)}]")

    user_content = "\n".join(blocks)

    try:
        client  = _get_claude()
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        content = message.content[0].text.strip()
        if "```" in content:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
            if m:
                content = m.group(1)
        result = json.loads(content)
        if result.get("text"):
            result["text"] = _apply_sttt_corrections(result["text"])

        # Prompt rules alone have not held on this pipeline: verify the callsign is actually
        # readable out of the transmission rather than trusting that it was.
        callsign = result.get("callsign")
        if callsign and not _callsign_supported_by_text(callsign, raw_text):
            print(f"  [callsign] dropped {callsign!r}: not spelled out in the transmission", flush=True)
            result["callsign"] = None

        return result
    except json.JSONDecodeError:
        return {"vessel": None, "callsign": None, "text": _apply_sttt_corrections(raw_text)}
    except Exception as exc:
        print(f"  [extract_vessel error] {exc}", flush=True)
        return {"vessel": None, "callsign": None, "text": _apply_sttt_corrections(raw_text)}


def enrich_with_ais(result: dict) -> dict:
    ais    = match_by_name(result.get("vessel"))
    method = "name"
    if not ais:
        ais    = match_by_callsign(result.get("callsign"))
        method = "callsign"
    if not ais:
        return result
    enriched = dict(result)
    enriched.update({
        "vessel": ais["name"], "mmsi": ais["mmsi"], "match_method": method,
        "type": ais.get("type"), "imo": ais.get("imo"),
        "length": ais.get("length"), "beam": ais.get("beam"),
        "latitude": ais.get("latitude"), "longitude": ais.get("longitude"),
        "sog": ais.get("sog"), "cog": ais.get("cog"), "heading": ais.get("heading"),
    })
    if ais.get("callsign"):
        enriched["callsign"] = ais["callsign"]
    return enriched


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
5. Shore stations (Maas Approach, Rotterdam VTS, Pilot) are never the vessel.
6. "evidence" is a short quote from the transmissions, or a one-line reason. Keep it factual.
7. Do NOT return transcriptions. You are identifying speakers, not transcribing.
"""


def _resolver_candidates(chunks: list[dict]) -> list[dict]:
    """AIS vessels plausibly involved in this window.

    Callsign matches come first and are marked: match_by_callsign is an exact dictionary
    lookup, so it is real evidence -- but only when the transmission actually spelled a
    callsign out. Otherwise the "exactness" is just an invented string that happened to
    exist, and the mark would launder a guess into evidence.
    """
    candidates: dict[str, dict] = {}

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

    for chunk in chunks:
        for hint in _find_ais_hints(chunk.get("text", "")):
            mmsi = hint.get("mmsi")
            if mmsi and mmsi not in candidates:
                candidates[mmsi] = dict(hint)

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


def _html_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_conversations_page(rows: list[dict]) -> str:
    """Render resolved exchanges, newest first. Built from stored data on every request."""
    blocks = []
    for row in reversed(rows):
        vessel = row.get("vessel")
        conf   = row.get("confidence", "low")
        ident  = _html_escape(vessel) if vessel else "unidentified"
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


def format_for_plugin(result: dict) -> str:
    parts = []
    vessel = result.get("vessel")
    vtype  = result.get("vessel_type")
    if vessel:
        parts.append(f"[{vessel}/{vtype}]" if vtype else f"[{vessel}]")
    if result.get("mmsi"):
        parts.append(f"(MMSI:{result['mmsi']})")
    elif result.get("callsign"):
        parts.append(f"({result['callsign']})")
    parts.append(result.get("text", ""))
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Whisper decoder parameters
# ---------------------------------------------------------------------------
# Transcription backends -- see stt_proxy/backends.py
#
# Imported as a module as well as by name: STT_BACKEND and the Groq settings are read
# inside that module, so tests patch them there.
# ---------------------------------------------------------------------------

from stt_proxy import backends  # noqa: E402
from stt_proxy.backends import (  # noqa: E402
    BACKEND_HOST,
    BACKEND_PORT,
    DEFAULT_MARITIME_PROMPT,
    GROQ_API_KEY,
    GROQ_HOST,
    GROQ_MODEL,
    GROQ_PATH,
    GROQ_PROMPT_MAX_WORDS,
    GROQ_QUOTA_WARN_AT,
    GROQ_QUOTA_WARN_STEP,
    GROQ_TIMEOUT_S,
    STT_BACKEND,
    STUCK_THRESHOLD_S,
    _build_groq_fields,
    _build_multipart,
    _build_whisper_params,
    _check_groq_quota,
    _client_response_headers,
    _effective_prompt,
    _env_bool,
    _parse_multipart,
    _parse_retry_after,
    _transcribe_groq,
    _transcribe_whisper_cpp,
    _truncate_prompt,
    _watchdog_loop,
    transcribe,
)




# ---------------------------------------------------------------------------
# Backend watchdog


# ---------------------------------------------------------------------------
# HTTP proxy
# ---------------------------------------------------------------------------

# The SDRSharp plugin sends to /v1/audio/transcriptions (OpenAI-compatible path).
# Each backend knows its own upstream path, so this is now just the set of paths the
# plugin is allowed to POST to.
PATH_MAP = frozenset({
    "/v1/audio/transcriptions",
    "/v1/audio/transcriptions/",
})


class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path in ("/", "/identified-vessels"):
            try:
                with open(VESSELS_LOG_FILE, "r", encoding="utf-8") as f:
                    data = f.read().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if self.path in ("/conversations", "/conversations/"):
            try:
                with _resolved_lock:
                    rows = list(_resolved)
                data = render_conversations_page(rows).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if self.path == "/api/conversations":
            try:
                with _resolved_lock:
                    data = json.dumps(list(_resolved)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if self.path == "/api/ais-cache":
            try:
                with _cache_lock:
                    entries = list(_vessel_cache.values())
                data = json.dumps(entries).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        self.send_response(404)
        self.end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        ts      = datetime.datetime.now().strftime("%H:%M:%S")
        channel = self.headers.get("X-Whisper-Channel", "").strip()
        mode    = self.headers.get("X-Whisper-Mode", "maritime").lower()

        # Transcription is the only thing this proxy accepts a POST for.
        if self.path not in PATH_MAP:
            self.send_response(404)
            self.end_headers()
            return

        # Keep only the audio file and the client's optional language/prompt overrides.
        # Every other decoder param is server-owned (see _build_whisper_params) so tuning
        # is a proxy restart rather than a plugin rebuild + SDR# restart.
        try:
            client_fields, file_info = _parse_multipart(self.headers.get("Content-Type", ""), body)
            if file_info is None:
                raise ValueError("no file part in request")
        except Exception as exc:
            print(f"[{ts}] malformed request body: {exc}", flush=True)
            self._send_json(400, {"error": f"malformed multipart request: {exc}"})
            return

        status, resp_body, resp_headers = transcribe(
            file_info,
            language=client_fields.get("language", ""),
            prompt=client_fields.get("prompt", ""),
        )

        # Post-process successful transcriptions
        if status == 200:
            try:
                data     = json.loads(resp_body)
                raw_text = data.get("text", "").strip()

                if _is_hallucination(raw_text):
                    print(f"[{ts}] [filtered] '{raw_text[:60]}'", flush=True)
                    data["text"] = ""
                    resp_body = json.dumps(data).encode("utf-8")

                elif _is_prompt_echo(raw_text, _effective_prompt(client_fields.get("prompt", ""))):
                    # Logged separately from [filtered]: an echo means the decoder returned
                    # the prompt instead of the audio, which is worth being able to spot in
                    # the log rather than having it disappear into the hallucination count.
                    print(f"[{ts}] [prompt-echo] '{raw_text[:60]}'", flush=True)
                    data["text"] = ""
                    resp_body = json.dumps(data).encode("utf-8")

                elif mode == "airband":
                    corrected = _apply_sttt_corrections(raw_text, mode="airband")
                    channel_label = f"[{channel} MHz]" if channel else "[airband]"
                    print(f"[{ts}] {channel_label} {corrected}", flush=True)
                    data["text"] = corrected
                    resp_body = json.dumps(data).encode("utf-8")

                elif channel in ("160.650", "160,650"):
                    # Maas Approach CH 01: full Claude extraction + AIS enrichment
                    result = extract_vessel(raw_text, channel)
                    result = enrich_with_ais(result)

                    # Maas response correlation
                    if _is_maas_response(raw_text) and result.get("vessel"):
                        fuzzy_entry, fuzzy_idx = _find_fuzzy_match_in_buffer(result.get("vessel"))
                        if fuzzy_entry:
                            old_v = fuzzy_entry["vessel"]
                            new_v = result.get("vessel")
                            print(f"  [correlation] '{old_v}' -> '{new_v}'", flush=True)
                            _update_buffer_entry(fuzzy_idx, new_v, result)

                    display_text = format_for_plugin(result)
                    _append_vessel_to_log(result, raw_text)
                    _add_to_buffer(result, raw_text, channel)
                    if CONVERSATION_RESOLVER:
                        # Journalled with the raw transcription. The retrospective pass reads
                        # this text and never writes it back.
                        _record_chunk(channel, raw_text, result)

                    vessel   = result.get("vessel") or "?"
                    vtype    = result.get("vessel_type") or "-"
                    mmsi     = result.get("mmsi") or "-"
                    callsign = result.get("callsign") or "-"
                    method   = f"/{result['match_method']}" if result.get("match_method") else ""
                    ais_size = f"  ais={_cache_size()}" if _cache_size() else ""
                    print(f"[{ts}] CH01: vessel={vessel}  type={vtype}  mmsi={mmsi}  cs={callsign}{method}{ais_size}", flush=True)
                    print(f"        {result.get('text', raw_text)}", flush=True)

                    data["text"] = display_text
                    resp_body = json.dumps(data).encode("utf-8")

                else:
                    # Other maritime channels: corrections only, no AI
                    corrected = _apply_sttt_corrections(raw_text)
                    channel_label = f"[{channel} MHz]" if channel else "[maritime]"
                    print(f"[{ts}] {channel_label} {corrected}", flush=True)
                    data["text"] = corrected
                    resp_body = json.dumps(data).encode("utf-8")

            except Exception as exc:
                print(f"[{ts}] post-process error: {exc}", flush=True)
        else:
            preview = resp_body[:120].decode("utf-8", errors="replace")
            print(f"[{ts}] {STT_BACKEND} HTTP {status}: {preview}", flush=True)

        self.send_response(status)
        for key, val in _client_response_headers(resp_headers):
            self.send_header(key, val)
        self.send_header("Content-Length", str(len(resp_body)))
        # The plugin reads until EOF, so the socket has to close for it to see anything.
        # Set explicitly rather than relying on the header filter above: this is the
        # contract WhisperClient depends on, and it should not be one stray upstream
        # header away from breaking again.
        self.close_connection = True
        self.end_headers()
        self.wfile.write(resp_body)

    def log_message(self, fmt, *args):
        pass  # suppress per-request access log noise


if __name__ == "__main__":
    if STT_BACKEND not in ("groq", "whisper_cpp"):
        raise SystemExit(
            f"STT_BACKEND={STT_BACKEND!r} is not valid — use 'groq' or 'whisper_cpp'."
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY not set — maritime vessel extraction disabled.")
    else:
        print("Anthropic API key: OK")

    if STT_BACKEND == "groq" and not GROQ_API_KEY:
        print("WARNING: GROQ_API_KEY not set — transcription will fail on every chunk.")

    _init_vessels_log()
    _load_cache()
    _load_conversations()

    if CONVERSATION_RESOLVER:
        threading.Thread(target=_conversation_reaper, daemon=True).start()
        atexit.register(_save_conversations)
        print(f"Conversation resolver: enabled (window gap {CONVERSATION_GAP_S}s) "
              f"-> http://localhost:{PROXY_PORT}/conversations", flush=True)
    else:
        print("Conversation resolver: disabled (CONVERSATION_RESOLVER=off)", flush=True)

    ais_key = os.environ.get("AISSTREAM_API_KEY", "")
    if ais_key:
        threading.Thread(target=_ais_thread, args=(ais_key,), daemon=True).start()
        threading.Thread(target=_periodic_save, daemon=True).start()
        atexit.register(_save_cache)
        print("AIS feed: starting...", flush=True)
    else:
        print("AIS feed: disabled (set AISSTREAM_API_KEY to enable)", flush=True)

    # The watchdog exists solely to kill and restart the local whisper-server when the
    # AMD driver wedges mid-inference. Under Groq there is no such process, and an armed
    # watchdog would shell into WSL to "restart" it on any slow API call.
    if STT_BACKEND == "whisper_cpp":
        threading.Thread(target=_watchdog_loop, daemon=True).start()
        print(f"Backend watchdog: enabled (stuck threshold {STUCK_THRESHOLD_S:.0f}s)", flush=True)
    else:
        print("Backend watchdog: disabled (not applicable to the groq backend)", flush=True)

    # Threaded: a single slow/failing external call (Claude API, backend decode) must not
    # block every other in-flight transcription request. Shared state (_vessel_buffer,
    # _vessel_cache, the vessels log file) is already lock-protected for the AIS/periodic-
    # save background threads, which already run concurrently with request handling.
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
    if STT_BACKEND == "groq":
        destination = f"groq {GROQ_HOST} ({GROQ_MODEL})"
    else:
        destination = f"whisper.cpp localhost:{BACKEND_PORT}"
    print(f"Whisper proxy  :  localhost:{PROXY_PORT}  ->  {destination}", flush=True)
    server.serve_forever()
