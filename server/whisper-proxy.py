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
BACKEND_HOST = os.environ.get("WHISPER_BACKEND_HOST", "localhost").strip() or "localhost"
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

# When the plugin last posted audio, and when this process started. Both epoch seconds.
# Chunk arrival -- not process liveness -- is what tells the control panel SDR# is actually
# receiving: SDR# can sit open with the play button unpressed and nothing would ever arrive,
# which a process-alive check would report as healthy.
_last_chunk_at: float | None = None
_STARTED_AT = time.time()

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
    _partial_callsign_candidates,
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
from stt_proxy import aishub  # noqa: E402
from stt_proxy.ais import (  # noqa: E402
    AIS_CACHE_FILE,
    AIS_HINT_FILTER,
    AIS_HINT_MIN_SCORE,
    AIS_SAVE_INTERVAL,
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
    match_by_callsign_pattern,
    match_by_callsign_suffix,
    match_by_mmsi,
    match_by_name,
)


# ---------------------------------------------------------------------------
# Per-transmission identification and the vessels log
#   see stt_proxy/identify.py and stt_proxy/vessel_log.py
# ---------------------------------------------------------------------------

from stt_proxy.identify import (  # noqa: E402
    SYSTEM_PROMPT,
    enrich_with_ais,
    extract_vessel,
    format_for_plugin,
)
from stt_proxy.vessel_log import (  # noqa: E402
    VESSELS_LOG_FILE,
    _append_vessel_to_log,
    _init_vessels_log,
)






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
    _decode_spoken_word,
    _is_hallucination,
    _is_prompt_echo,
    PHONETIC_PROBE_MIN_LEN,
    _partial_callsign_pattern,
    _phonetic_callsign_probes,
    _prompt_echo_tokens,
    _spelled_out_runs,
)




# Callsign verification


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


def _status_payload() -> dict:
    """What the control panel needs, and nothing else.

    Every field here reaches a browser over a network, so the key set is pinned by a test.
    Read through the modules rather than the re-exports: the feed thread rebinds
    ais._vessel_cache, so an imported name would freeze a snapshot.
    """
    with ais._cache_lock:
        cache_size = len(ais._vessel_cache)
    with conversations._resolved_lock:
        stored = len(conversations._resolved)
    last_poll = ais._last_poll_at
    return {
        "stt_backend": STT_BACKEND,
        "ais_source": os.environ.get("AIS_SOURCE", "aishub").strip().lower(),
        "ais_cache_size": cache_size,
        "ais_last_poll_at": last_poll.timestamp() if last_poll else None,
        # Whether the vessel feed is actually alive, which the age above cannot say on its
        # own: a poll that has not succeeded for an hour and one that succeeds every fifteen
        # minutes both look like "an old timestamp" until you know what the interval is and
        # whether the last attempt failed. The username is scrubbed from `last_error` inside
        # aishub before it can reach here.
        "aishub": aishub.feed_status(),
        "conversations": stored,
        "last_chunk_at": _last_chunk_at,
        "started_at": _STARTED_AT,
        "now": time.time(),
    }


class ProxyHandler(http.server.BaseHTTPRequestHandler):

    # Everything served here is live state that changes second to second, and none of it was
    # sending a cache directive -- no Cache-Control, no Expires, no ETag, no Last-Modified.
    # A response carrying no freshness information at all may be cached heuristically, and
    # /conversations self-refreshes with <meta http-equiv="refresh">, which is an ordinary
    # navigation and so consults the HTTP cache. Observed directly: the server answering 157
    # exchanges while the browser sat on 156, having reloaded on schedule and been handed its
    # own cached copy -- indistinguishable, from the outside, from a page that never reloads.
    # Pragma is there because this server still speaks HTTP/1.0.
    def _send_live_headers(self, content_type: str, length: int, cors: bool = False) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/identified-vessels"):
            try:
                with open(VESSELS_LOG_FILE, "r", encoding="utf-8") as f:
                    data = f.read().encode("utf-8")
                self.send_response(200)
                self._send_live_headers("text/html; charset=utf-8", len(data))
                self.wfile.write(data)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if self.path in ("/conversations", "/conversations/"):
            try:
                with conversations._resolved_lock:
                    rows = list(conversations._resolved)
                data = render_conversations_page(rows).encode("utf-8")
                self.send_response(200)
                self._send_live_headers("text/html; charset=utf-8", len(data))
                self.wfile.write(data)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if self.path == "/api/conversations":
            try:
                with conversations._resolved_lock:
                    data = json.dumps(list(conversations._resolved)).encode("utf-8")
                self.send_response(200)
                self._send_live_headers("application/json", len(data), cors=True)
                self.wfile.write(data)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if self.path == "/api/ais-cache":
            try:
                # Through the module, not `from ais import _vessel_cache`: the feed thread
                # rebinds these, so an imported name would freeze a snapshot (see ais.py).
                with ais._cache_lock:
                    entries = list(ais._vessel_cache.values())
                data = json.dumps(entries).encode("utf-8")
                self.send_response(200)
                self._send_live_headers("application/json", len(data), cors=True)
                self.wfile.write(data)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if self.path == "/api/status":
            try:
                data = json.dumps(_status_payload()).encode("utf-8")
                self.send_response(200)
                self._send_live_headers("application/json", len(data), cors=True)
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

        # A chunk arrived, so SDR# is open AND playing. The dashboard reads this.
        global _last_chunk_at
        _last_chunk_at = time.time()

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
                    result = enrich_with_ais(result, raw_text)

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

    ais_source = os.environ.get("AIS_SOURCE", "aishub").strip().lower()
    if ais_source == "aishub":
        aishub_user = os.environ.get("AISHUB_USERNAME", "")
        if aishub_user:
            aishub.start(aishub_user)
            threading.Thread(target=_periodic_save, daemon=True).start()
            atexit.register(_save_cache)
            print(f"AIS feed: AISHub, box {aishub.BBOX}, every {aishub.POLL_SEC}s", flush=True)
        else:
            print("AIS feed: disabled (AIS_SOURCE=aishub but AISHUB_USERNAME is unset)",
                  flush=True)
    elif ais_source == "aisstream":
        # Kept live and tested rather than commented out. aisstream was a reliable free
        # source for a long time and may return; code that is not exercised does not work
        # when it is reverted to.
        ais_key = os.environ.get("AISSTREAM_API_KEY", "")
        if ais_key:
            threading.Thread(target=_ais_thread, args=(ais_key,), daemon=True).start()
            threading.Thread(target=_periodic_save, daemon=True).start()
            atexit.register(_save_cache)
            print("AIS feed: aisstream, starting...", flush=True)
        else:
            print("AIS feed: disabled (AIS_SOURCE=aisstream but AISSTREAM_API_KEY is unset)",
                  flush=True)
    else:
        print(f"AIS feed: disabled (AIS_SOURCE={ais_source})", flush=True)

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
