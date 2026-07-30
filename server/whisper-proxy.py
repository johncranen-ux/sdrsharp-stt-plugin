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

# ---------------------------------------------------------------------------
# Recent-traffic memory and retrospective conversation resolution
#   see stt_proxy/conversations.py and stt_proxy/claude.py
# ---------------------------------------------------------------------------

from stt_proxy import conversations  # noqa: E402
from stt_proxy.claude import _get_claude  # noqa: E402
from stt_proxy.conversations import (  # noqa: E402
    CONVERSATION_GAP_S,
    CONVERSATION_MAX_CHUNKS,
    CONVERSATION_POLL_S,
    CONVERSATION_RESOLVER,
    CONVERSATIONS_FILE,
    RESOLVER_SYSTEM_PROMPT,
    VESSEL_BUFFER_TTL,
    _add_to_buffer,
    _conversation_reaper,
    _find_fuzzy_match_in_buffer,
    _html_escape,
    _is_maas_response,
    _load_conversations,
    _record_chunk,
    _render_resolver_input,
    _resolver_candidates,
    _resolve_window,
    _save_conversations,
    _split_windows,
    _store_resolved,
    _take_closed_windows,
    _unresolved,
    _update_buffer_entry,
    _validate_exchanges,
    render_conversations_page,
    resolve_conversation,
)



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
