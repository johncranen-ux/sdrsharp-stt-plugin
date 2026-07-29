"""
Whisper STT proxy — listens on :9000, forwards to whisper.cpp server on :8080 (WSL2),
post-processes maritime transcriptions with Claude to extract vessel names and callsigns,
fuzzy-matches against a live AIS feed (aisstream.io) for Rotterdam / Maas Approach,
and returns enriched text to the SDRSharp plugin.

Usage:
    py whisper-proxy.py

Required env vars:
    ANTHROPIC_API_KEY   — Claude API key (for maritime vessel extraction)
    AISSTREAM_API_KEY   — aisstream.io API key (free at aisstream.io)

Optional env vars:
    WHISPER_BACKEND_PORT  — override backend port (default: 8080)
    PROXY_PORT            — override proxy listen port (default: 9000)
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

# Rotterdam / Maas Approach bounding box  [SW corner, NE corner]
ROTTERDAM_BBOX = [[[51.0, 2.95], [52.85, 6.0]]]

AIS_CACHE_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ais_cache.json")
VESSELS_LOG_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "identified_vessels.html")
AIS_SAVE_INTERVAL   = 300
VESSEL_BUFFER_TTL   = 60

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


def _add_to_buffer(result: dict, raw_text: str) -> None:
    with _buffer_lock:
        now = datetime.datetime.now()
        _vessel_buffer.append({
            "time": now,
            "vessel": result.get("vessel"),
            "fuzzy": result.get("match_method") == "name_fuzzy",
            "result": result,
            "raw_text": raw_text,
        })
        cutoff = now - datetime.timedelta(seconds=VESSEL_BUFFER_TTL)
        _vessel_buffer[:] = [e for e in _vessel_buffer if e["time"] > cutoff]


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

AIS_SHIP_TYPES = {
    30: "Fishing", 31: "Tug", 32: "Tug", 33: "Military ops", 34: "Dive ops",
    35: "Medical transport", 36: "Sailing", 37: "Pleasure craft",
    40: "Pilot vessel", 41: "Search & rescue", 42: "Tug", 43: "Port tender",
    50: "Pilot vessel", 51: "Search & rescue", 52: "Tug", 53: "Port tender",
    60: "Passenger ship", 61: "Cargo ship", 62: "Tanker", 70: "Tanker",
    71: "Tanker", 72: "Tanker", 73: "Tanker", 74: "Tanker", 75: "Tanker",
    76: "Tanker", 77: "Tanker", 78: "Tanker", 80: "General cargo",
    81: "General cargo", 82: "General cargo", 83: "General cargo",
    84: "General cargo", 85: "General cargo", 86: "General cargo",
    87: "General cargo", 88: "General cargo", 89: "Other",
    90: "Container ship", 91: "Container ship", 92: "Container ship",
    93: "Container ship", 94: "Container ship", 95: "Container ship",
    96: "Container ship", 97: "Container ship", 98: "Container ship",
    100: "Bulk carrier", 101: "Bulk carrier", 102: "Bulk carrier",
    103: "Bulk carrier", 104: "Bulk carrier", 105: "Bulk carrier",
}

def _get_ship_type_name(type_code) -> str | None:
    if type_code is None:
        return None
    return AIS_SHIP_TYPES.get(type_code, f"Type {type_code}")


# ---------------------------------------------------------------------------
# AIS vessel cache
# ---------------------------------------------------------------------------

_vessel_cache:   dict[str, dict] = {}
_callsign_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


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


def _load_cache() -> None:
    global _vessel_cache, _callsign_cache
    try:
        with open(AIS_CACHE_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)
        with _cache_lock:
            for entry in entries:
                _vessel_cache[entry["name"].upper()] = entry
                if entry.get("callsign"):
                    _callsign_cache[entry["callsign"].upper()] = entry
        print(f"[AIS] loaded {len(_vessel_cache)} vessels from cache", flush=True)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[AIS] cache load error: {exc}", flush=True)


def _save_cache() -> None:
    try:
        with _cache_lock:
            entries = list(_vessel_cache.values())
        with open(AIS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f)
    except Exception as exc:
        print(f"[AIS] cache save error: {exc}", flush=True)


def _periodic_save() -> None:
    while True:
        threading.Event().wait(AIS_SAVE_INTERVAL)
        _save_cache()


def _ais_thread(api_key: str) -> None:
    import asyncio
    asyncio.run(_ais_loop(api_key))


async def _ais_loop(api_key: str) -> None:
    try:
        import websockets
    except ImportError:
        print("[AIS] 'websockets' not installed — run: pip install websockets", flush=True)
        return

    sub_msg = json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": ROTTERDAM_BBOX,
        "FilterMessageTypes": ["ShipStaticData", "PositionReport"],
    })

    import ssl as _ssl
    import certifi
    ssl_ctx = _ssl.create_default_context(cafile=certifi.where())

    while True:
        try:
            async with websockets.connect("wss://stream.aisstream.io/v0/stream", ssl=ssl_ctx) as ws:
                await ws.send(sub_msg)
                print("[AIS] connected — watching Rotterdam / Maas Approach area", flush=True)
                async for raw in ws:
                    _process_ais(json.loads(raw))
        except Exception as exc:
            print(f"[AIS] disconnected ({exc}), reconnecting in 30s...", flush=True)
            import asyncio as _a
            await _a.sleep(30)


def _process_ais(msg: dict) -> None:
    try:
        msg_type = msg.get("MessageType", "")
        meta     = msg.get("MetaData", {})
        mmsi     = str(meta.get("MMSI", "")).strip()
        if not mmsi:
            return

        if msg_type == "ShipStaticData":
            ship     = msg.get("Message", {}).get("ShipStaticData", {})
            name     = (ship.get("Name") or meta.get("ShipName") or "").strip()
            callsign = ship.get("CallSign", "").strip()
            if name:
                dim    = ship.get("Dimension", {})
                imo    = ship.get("IMO")
                stype  = ship.get("Type")
                length = (dim.get("A", 0) + dim.get("B", 0)) or None
                beam   = (dim.get("C", 0) + dim.get("D", 0)) or None
                entry  = {"name": name, "callsign": callsign, "mmsi": mmsi,
                          "type": stype, "imo": imo, "length": length, "beam": beam}
                with _cache_lock:
                    _vessel_cache[name.upper()] = entry
                    if callsign:
                        _callsign_cache[callsign.upper()] = entry

        elif msg_type == "PositionReport":
            name = meta.get("ShipName", "").strip()
            if name:
                key = name.upper()
                pos = msg.get("Message", {}).get("PositionReport", {})
                lat = pos.get("Latitude")
                lon = pos.get("Longitude")
                sog = pos.get("SOG")
                cog = pos.get("COG")
                heading = pos.get("TrueHeading")
                with _cache_lock:
                    if key not in _vessel_cache:
                        _vessel_cache[key] = {"name": name, "callsign": "", "mmsi": mmsi,
                                              "type": None, "imo": None, "length": None, "beam": None,
                                              "latitude": lat, "longitude": lon, "sog": sog,
                                              "cog": cog, "heading": heading}
                    else:
                        e = _vessel_cache[key]
                        e["latitude"] = lat; e["longitude"] = lon
                        e["sog"] = sog; e["cog"] = cog; e["heading"] = heading
    except Exception as exc:
        print(f"[AIS] process error: {exc}", flush=True)


def _cache_size() -> int:
    with _cache_lock:
        return len(_vessel_cache)


def match_by_name(extracted_name: str) -> dict | None:
    if not extracted_name:
        return None
    query = extracted_name.upper()
    with _cache_lock:
        if not _vessel_cache:
            return None
        keys  = list(_vessel_cache.keys())
        cache = dict(_vessel_cache)
    hit = rf_process.extractOne(query, keys, scorer=rf_fuzz.WRatio, score_cutoff=80)
    if hit:
        return cache[hit[0]]
    SKIP = {"MV", "MT", "MS", "SV", "SS", "TUG", "MOTOR", "TANKER",
            "BULKER", "VESSEL", "CONTAINER", "MOTORTANKER", "MOTORVESSEL"}
    words = [w for w in query.split() if w not in SKIP and len(w) >= 3]
    candidates = []
    for length in range(len(words), 0, -1):
        for start in range(len(words) - length + 1):
            candidates.append(" ".join(words[start:start + length]))
    for candidate in candidates:
        hit = rf_process.extractOne(candidate, keys, scorer=rf_fuzz.WRatio, score_cutoff=88)
        if hit:
            return cache[hit[0]]
    return None


def match_by_callsign(extracted_callsign: str) -> dict | None:
    if not extracted_callsign:
        return None
    with _cache_lock:
        return _callsign_cache.get(extracted_callsign.upper())


def _find_ais_hints(text: str, n: int = 5) -> list[dict]:
    words = text.upper().split()
    if not words:
        return []
    with _cache_lock:
        if not _vessel_cache:
            return []
        keys  = list(_vessel_cache.keys())
        cache = dict(_vessel_cache)
    seen:    set[str]  = set()
    results: list[dict] = []
    probes = []
    for i, w in enumerate(words):
        if len(w) >= 3:
            probes.append(w)
        if i < len(words) - 1 and len(words[i + 1]) >= 3:
            probes.append(f"{w} {words[i + 1]}")
    for probe in probes:
        hit = rf_process.extractOne(probe, keys, scorer=rf_fuzz.WRatio, score_cutoff=65)
        if hit:
            entry = cache[hit[0]]
            mmsi  = entry.get("mmsi", "")
            if mmsi and mmsi not in seen:
                seen.add(mmsi)
                results.append(entry)
                if len(results) >= n:
                    break
    return results


# ---------------------------------------------------------------------------
# Hallucination filter
# ---------------------------------------------------------------------------

_HALLUCINATION_EXACT = {
    "you", "hmm", "hm", "ah", "uh", "um",
    "thank you", "thanks",
    "thank you for watching", "thanks for watching",
    "please subscribe", "subscribe",
    "bye", "goodbye",
}

_HALLUCINATION_PATTERNS = [
    re.compile(r'^\s*[.\s]+\s*$'),                   # only dots / whitespace
    re.compile(r'^\s*[\W\s]+\s*$'),                  # only punctuation
    re.compile(r'^(\w[\w\s]*?)\s*(\1\s*){3,}$', re.IGNORECASE),  # phrase repeated 4+ times
]


def _is_hallucination(text: str) -> bool:
    t = text.strip()
    if not t or len(t) < 2:
        return True
    t_lower = t.lower().rstrip('.,!?').strip()
    if t_lower in _HALLUCINATION_EXACT:
        return True
    for pat in _HALLUCINATION_PATTERNS:
        if pat.match(t):
            return True
    words = t_lower.split()
    if len(words) >= 4 and len(set(words)) == 1:
        return True
    return False


# ---------------------------------------------------------------------------
# Post-processing corrections
# ---------------------------------------------------------------------------

def _apply_sttt_corrections(text: str) -> str:
    corrections = [
        (r'\bmass\s+approach\b', 'Maas Approach', re.IGNORECASE),
        (r'\bmarch\s+approach\b', 'Maas Approach', re.IGNORECASE),
        (r'\bmars\s+approach\b', 'Maas Approach', re.IGNORECASE),
        (r'\bmass\b(?=\s)', 'Maas', re.IGNORECASE),
        (r'\bmars\b(?=\s)', 'Maas', re.IGNORECASE),
        (r'\bcosine\b', 'Callsign', re.IGNORECASE),
        (r'\bcall\s*sign\b', 'Callsign', re.IGNORECASE),
        (r'\bmotor\s+tanker\b', 'Motortanker', re.IGNORECASE),
        (r'\bdraft\b', 'draught', re.IGNORECASE),
        (r'\bboys\b', 'buoys', re.IGNORECASE),
        (r'\bboy\b', 'buoy', re.IGNORECASE),
    ]
    result = text
    for pattern, replacement, flags in corrections:
        result = re.sub(pattern, replacement, result, flags=flags)
    return result


# ---------------------------------------------------------------------------
# Claude vessel-extraction agent
# ---------------------------------------------------------------------------

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


SYSTEM_PROMPT = """\
You analyse VHF marine radio transcriptions from Rotterdam harbour (Maas Approach / Rotterdam VTS area).
Correct fuzzy STT errors using maritime context. Return ONLY raw JSON, no markdown:
{"vessel": "<name or null>", "callsign": "<callsign or null>", "vessel_type": "<type or null>", "text": "<corrected text>"}

Rules:
1. Shore stations (Maas Approach, Rotterdam VTS, Pilot) are NOT vessels.
2. Extract vessel names: after "this is", "calling", vessel type words, or when shore station addresses a vessel.
3. Always extract callsigns (4-letter codes, MMSI 9-digit, alphanumeric like 9HF5093).
4. Correct STT errors: mass->maas, draft->draught, boys->buoys, motor tanker->Motortanker.
5. vessel_type: tanker/bulker/container/tug/ferry/general_cargo/passenger/yacht/pilot/null.
6. If [AIS: ...] hints are given, use them to correct vessel names (score>=80% match or 2+ shared tokens).
7. In "text" field, return the full corrected transcription.
"""


def extract_vessel(raw_text: str) -> dict:
    hints = _find_ais_hints(raw_text)
    if hints:
        hint_parts = []
        for h in hints:
            parts = [f"{h['name']} (MMSI:{h['mmsi']})"]
            if h.get("callsign"):
                parts.append(f"cs:{h['callsign']}")
            if h.get("type"):
                parts.append(f"type:{_get_ship_type_name(h['type'])}")
            hint_parts.append(" ".join(parts))
        user_content = f"{raw_text}\n[AIS: {', '.join(hint_parts)}]"
    else:
        user_content = raw_text

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
#
# Owned here rather than by the C# client: tuning beam size, VAD, or hallucination
# suppression is then a proxy restart, not a plugin rebuild/redeploy/SDR# restart.
# All are env-overridable for A/B testing with server/bench.py without editing code.
# ---------------------------------------------------------------------------

DEFAULT_MARITIME_PROMPT = (
    "Maas Approach, this is Motortanker Neptune, callsign PABC, requesting permission "
    "to enter the Botlek, over. "
    "Motortanker Neptune, Maas Approach, roger, proceed to VHF channel six one, out. "
    "Rotterdam VTS, be advised we are standing by on channel one six, over."
)


def _env_bool(name: str, default: str) -> str:
    # whisper.cpp's form-field parser expects the literal strings "true"/"false".
    return "true" if os.environ.get(name, default).strip().lower() in ("1", "true", "yes") else "false"


def _build_whisper_params(client_language: str, client_prompt: str) -> dict:
    return {
        "temperature": os.environ.get("WHISPER_TEMPERATURE", "0"),
        "beam_size": os.environ.get("WHISPER_BEAM_SIZE", "5"),
        "best_of": os.environ.get("WHISPER_BEST_OF", "5"),
        "suppress_nst": _env_bool("WHISPER_SUPPRESS_NST", "true"),
        "response_format": "json",
        "language": client_language or os.environ.get("WHISPER_LANGUAGE", "en"),
        "prompt": client_prompt or os.environ.get("WHISPER_PROMPT", DEFAULT_MARITIME_PROMPT),
        "carry_initial_prompt": "true",
        # Off by default: server/bench.py on 49 real captures showed VAD-on configs
        # (48.5%/41.8% pooled WER) doing no better than, or worse than, the equivalent
        # VAD-off config (beam5_prompt, 40.8%) -- combined with the VAD+beam flakiness
        # bugs found in whisper.cpp itself (intermittent 500s, a full server wedge), the
        # data doesn't support leaving this on. The plugin's own client-side VAD (pre-roll,
        # adaptive threshold, squelch-aware) already does this job.
        "vad": _env_bool("WHISPER_VAD", "false"),
        "vad_threshold": os.environ.get("WHISPER_VAD_THRESHOLD", "0.5"),
        "vad_min_speech_duration_ms": os.environ.get("WHISPER_VAD_MIN_SPEECH_MS", "250"),
        "vad_min_silence_duration_ms": os.environ.get("WHISPER_VAD_MIN_SILENCE_MS", "100"),
        "vad_speech_pad_ms": os.environ.get("WHISPER_VAD_SPEECH_PAD_MS", "100"),
    }


# ---------------------------------------------------------------------------
# Multipart parse / rebuild
#
# The incoming request carries the client's params (temperature, language, prompt) plus
# the audio file. We keep only the file (and read language/prompt as optional client
# overrides) and rebuild the body with the server-owned params above.
# ---------------------------------------------------------------------------

def _parse_multipart(content_type_header: str, body: bytes):
    """Returns (fields: dict[str,str], file_info: dict | None).
    file_info has keys: field, filename, content_type, data.
    Raises ValueError if the body isn't a well-formed multipart/form-data message.
    """
    if "boundary=" not in content_type_header:
        raise ValueError("no boundary in Content-Type")

    from email.parser import BytesParser

    header_bytes = f"Content-Type: {content_type_header}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    msg = BytesParser().parsebytes(header_bytes + body)
    if not msg.is_multipart():
        raise ValueError("body is not multipart")

    fields: dict[str, str] = {}
    file_info = None
    for part in msg.get_payload():
        name = part.get_param("name", header="content-disposition")
        if name is None:
            continue
        filename = part.get_filename()
        if filename:
            file_info = {
                "field": name,
                "filename": filename,
                "content_type": part.get_content_type() or "audio/wav",
                "data": part.get_payload(decode=True) or b"",
            }
        else:
            payload = part.get_payload(decode=True) or b""
            fields[name] = payload.decode("utf-8", errors="replace").strip()

    return fields, file_info


def _build_multipart(fields: dict, file_info: dict) -> tuple[str, bytes]:
    boundary = "----WhisperProxy" + os.urandom(12).hex()
    parts = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_info["field"]}"; '
        f'filename="{file_info["filename"]}"\r\nContent-Type: {file_info["content_type"]}\r\n\r\n'.encode("utf-8")
        + file_info["data"]
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return boundary, b"".join(parts)


# ---------------------------------------------------------------------------
# Backend watchdog
#
# whisper-server can hang indefinitely on GPU inference -- confirmed to be a
# per-request-random AMD ROCm/gfx1100 driver race (matches the long-standing,
# unresolved github.com/ROCm/ROCm#2689), not tied to any specific decode
# setting, audio content, or idle timing we've been able to identify. It
# cannot be prevented from here. What we CAN do is stop the plugin from
# sitting on a dead request for the full client-side timeout: track how long
# each backend call has been in flight, and if one exceeds STUCK_THRESHOLD_S,
# kill and restart whisper-server so the stuck socket drops (the blocked
# request thread gets a connection error immediately and returns a fast
# error to the client instead of hanging) and the next real request gets a
# fresh, working server ~15-20s later while it reloads.
# ---------------------------------------------------------------------------

STUCK_THRESHOLD_S  = float(os.environ.get("WHISPER_WATCHDOG_STUCK_S", "25"))
WATCHDOG_POLL_S    = 3.0
RESTART_COOLDOWN_S = 5.0

_inflight_lock = threading.Lock()
_inflight_requests: dict[int, float] = {}
_inflight_next_id = 0

_restarting = threading.Event()


def _inflight_begin() -> int:
    global _inflight_next_id
    with _inflight_lock:
        _inflight_next_id += 1
        req_id = _inflight_next_id
        _inflight_requests[req_id] = time.monotonic()
    return req_id


def _inflight_end(req_id: int) -> None:
    with _inflight_lock:
        _inflight_requests.pop(req_id, None)


def _oldest_inflight_age() -> float | None:
    with _inflight_lock:
        if not _inflight_requests:
            return None
        return time.monotonic() - min(_inflight_requests.values())


def _restart_backend() -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [watchdog] backend stuck >{STUCK_THRESHOLD_S:.0f}s, restarting whisper-server...", flush=True)
    try:
        # whisper-server (cpp-httplib) sets SO_REUSEPORT, so a hung instance can
        # coexist with a freshly restarted one instead of erroring on bind -- the
        # kernel then load-balances requests across both, silently routing some
        # fraction to the dead one forever. Find+kill every PID on :8080 in one
        # combined call (fewer WSL interop round trips = less likely to time out
        # under the load a live GPU driver TDR event can put on WSL) with a
        # generous timeout. This is a best-effort fast path only -- the real
        # backstop is that start-whisper-server.sh itself self-cleans any stale
        # listener before binding, so it's safe to proceed to restart below even
        # if this step fails or times out.
        result = subprocess.run(
            ["wsl", "-d", "Ubuntu-22.04", "--", "bash", "-lc",
             "pids=$(ss -ltnp 2>/dev/null | grep ':8080' | grep -oP 'pid=\\K[0-9]+' | sort -u); "
             "[ -n \"$pids\" ] && kill -9 $pids; echo \"$pids\""],
            capture_output=True, text=True, timeout=30,
        )
        pids = result.stdout.split()
        if pids:
            print(f"[{ts}] [watchdog] killed whisper-server pid(s)={','.join(pids)}", flush=True)
        else:
            print(f"[{ts}] [watchdog] no listener found on :8080 to kill", flush=True)
    except Exception as exc:
        print(f"[{ts}] [watchdog] kill step failed (start-whisper-server.sh will self-clean): {exc}", flush=True)

    try:
        subprocess.Popen(
            ["wsl", "-d", "Ubuntu-22.04", "--", "bash", "-l", "-c", "~/start-whisper-server.sh"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"[{ts}] [watchdog] whisper-server restart issued (model reload takes ~15-20s)", flush=True)
    except Exception as exc:
        print(f"[{ts}] [watchdog] restart failed: {exc}", flush=True)


def _watchdog_loop() -> None:
    while True:
        threading.Event().wait(WATCHDOG_POLL_S)
        if _restarting.is_set():
            continue
        age = _oldest_inflight_age()
        if age is not None and age >= STUCK_THRESHOLD_S:
            _restarting.set()
            try:
                _restart_backend()
            finally:
                threading.Event().wait(RESTART_COOLDOWN_S)
                _restarting.clear()


# ---------------------------------------------------------------------------
# HTTP proxy
# ---------------------------------------------------------------------------

# The SDRSharp plugin sends to /v1/audio/transcriptions (OpenAI-compatible path).
# whisper.cpp server uses /inference. This map translates the path.
PATH_MAP = {
    "/v1/audio/transcriptions": "/inference",
    "/v1/audio/transcriptions/": "/inference",
}


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

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        # Translate OpenAI-style path to whisper.cpp path
        backend_path = PATH_MAP.get(self.path, self.path)

        forward_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("host", "content-length", "content-type")
        }
        content_type = self.headers.get("Content-Type", "")

        # Rewrite the multipart body: drop the client's decoder params, keep only the
        # audio file (plus optional language/prompt overrides), inject the server-owned
        # WHISPER_PARAMS. Falls back to forwarding the original body unchanged if the
        # request isn't parseable multipart, so a malformed request still gets a response
        # instead of silently vanishing.
        if backend_path == "/inference":
            try:
                client_fields, file_info = _parse_multipart(content_type, body)
                if file_info is None:
                    raise ValueError("no file part in request")
                params = _build_whisper_params(
                    client_language=client_fields.get("language", ""),
                    client_prompt=client_fields.get("prompt", ""),
                )
                boundary, body = _build_multipart(params, file_info)
                content_type = f"multipart/form-data; boundary={boundary}"
            except Exception as exc:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] multipart rewrite failed, forwarding original body: {exc}", flush=True)

        forward_headers["Content-Type"] = content_type
        forward_headers["Content-Length"] = str(len(body))

        # Forward to whisper.cpp backend. whisper.cpp's server has been observed to
        # non-deterministically return HTTP 500 "failed to process audio" for the exact
        # same request/audio that succeeds moments later when VAD + beam search are both
        # enabled — a flakiness bug in the backend, not something fixable here. Since the
        # same request often succeeds on retry, one immediate retry is a cheap mitigation.
        #
        # It can also hang outright (see the watchdog above) rather than error, so every
        # attempt is tracked in _inflight_requests for the watchdog thread to see; killing
        # the backend mid-hang drops this socket and turns the exception below into a fast
        # 503 instead of a multi-minute wait.
        max_attempts = 2 if backend_path == "/inference" else 1
        resp = resp_body = None
        for attempt in range(1, max_attempts + 1):
            req_id = _inflight_begin() if backend_path == "/inference" else None
            try:
                conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=90)
                conn.request("POST", backend_path, body=body, headers=forward_headers)
                resp      = conn.getresponse()
                resp_body = resp.read()
                conn.close()
            except Exception as exc:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] backend error (attempt {attempt}/{max_attempts}): {exc}", flush=True)
                error_msg = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(error_msg)))
                self.end_headers()
                self.wfile.write(error_msg)
                return
            finally:
                if req_id is not None:
                    _inflight_end(req_id)

            if resp.status < 500 or attempt == max_attempts:
                break
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] backend HTTP {resp.status}, retrying ({attempt}/{max_attempts})...", flush=True)

        ts      = datetime.datetime.now().strftime("%H:%M:%S")
        channel = self.headers.get("X-Whisper-Channel", "").strip()
        mode    = self.headers.get("X-Whisper-Mode", "maritime").lower()

        # Post-process successful transcriptions
        if resp.status == 200 and backend_path == "/inference":
            try:
                data     = json.loads(resp_body)
                raw_text = data.get("text", "").strip()

                if _is_hallucination(raw_text):
                    print(f"[{ts}] [filtered] '{raw_text[:60]}'", flush=True)
                    data["text"] = ""
                    resp_body = json.dumps(data).encode("utf-8")

                elif mode == "airband":
                    corrected = _apply_sttt_corrections(raw_text)
                    channel_label = f"[{channel} MHz]" if channel else "[airband]"
                    print(f"[{ts}] {channel_label} {corrected}", flush=True)
                    data["text"] = corrected
                    resp_body = json.dumps(data).encode("utf-8")

                elif channel in ("160.650", "160,650"):
                    # Maas Approach CH 01: full Claude extraction + AIS enrichment
                    result = extract_vessel(raw_text)
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
                    _add_to_buffer(result, raw_text)

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

        elif resp.status == 200:
            # Pass through non-transcription responses unchanged
            pass
        else:
            preview = resp_body[:120].decode("utf-8", errors="replace")
            print(f"[{ts}] backend HTTP {resp.status}: {preview}", flush=True)

        self.send_response(resp.status)
        for key, val in resp.getheaders():
            if key.lower() not in ("transfer-encoding", "content-length"):
                self.send_header(key, val)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def log_message(self, fmt, *args):
        pass  # suppress per-request access log noise


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY not set — maritime vessel extraction disabled.")
    else:
        print("Anthropic API key: OK")

    _init_vessels_log()
    _load_cache()

    ais_key = os.environ.get("AISSTREAM_API_KEY", "")
    if ais_key:
        threading.Thread(target=_ais_thread, args=(ais_key,), daemon=True).start()
        threading.Thread(target=_periodic_save, daemon=True).start()
        atexit.register(_save_cache)
        print("AIS feed: starting...", flush=True)
    else:
        print("AIS feed: disabled (set AISSTREAM_API_KEY to enable)", flush=True)

    threading.Thread(target=_watchdog_loop, daemon=True).start()
    print(f"Backend watchdog: enabled (stuck threshold {STUCK_THRESHOLD_S:.0f}s)", flush=True)

    # Threaded: a single slow/failing external call (Claude API, backend decode) must not
    # block every other in-flight transcription request. Shared state (_vessel_buffer,
    # _vessel_cache, the vessels log file) is already lock-protected for the AIS/periodic-
    # save background threads, which already run concurrently with request handling.
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
    print(f"Whisper proxy  :  localhost:{PROXY_PORT}  ->  backend localhost:{BACKEND_PORT}", flush=True)
    server.serve_forever()
