"""Speech-to-text backends, and everything that shapes a request to one.

Two interchangeable implementations behind `transcribe()`: Groq's hosted Whisper (the
default) and a local whisper.cpp server. Both return the same `{"text": ...}` envelope, so
nothing downstream knows or cares which ran.

Decoder parameters live here rather than in the plugin. Tuning is then a proxy restart
instead of a plugin rebuild, a redeploy and an SDR# restart -- which matters because the
plugin is the awkward half to change.

Also holds the watchdog for the local backend. It exists because the GPU this was developed
against hangs mid-inference (an AMD/ROCm fault, not anything fixable here); it arms only
when that backend is selected, since under Groq there is no process to restart.
"""

import datetime
import http.client
import json
import os
import re
import subprocess
import threading
import time

BACKEND_HOST = "localhost"
BACKEND_PORT = int(os.environ.get("WHISPER_BACKEND_PORT", "8080"))

# Which backend transcribes audio. The local GPU path is kept fully working as a fallback;
# see docs/design-notes.md for why the cloud is the default.
STT_BACKEND = os.environ.get("STT_BACKEND", "groq").strip().lower()

GROQ_HOST      = "api.groq.com"
GROQ_PATH      = "/openai/v1/audio/transcriptions"
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL     = os.environ.get("GROQ_MODEL", "whisper-large-v3").strip()
GROQ_TIMEOUT_S = float(os.environ.get("GROQ_TIMEOUT_S", "30"))

# Groq documents a 224-token cap on `prompt`. Tokens are not countable here without a
# tokenizer, so the cap is on words with enough slack for a worst-case ~1.5 tokens/word.
GROQ_PROMPT_MAX_WORDS = int(os.environ.get("GROQ_PROMPT_MAX_WORDS", "140"))

# How long a 429 may ask us to wait before the chunk is abandoned. The plugin sends chunks
# one at a time, so sleeping here stalls everything queued behind this one.
GROQ_MAX_RETRY_WAIT_S = float(os.environ.get("GROQ_MAX_RETRY_WAIT_S", "5"))

GROQ_QUOTA_WARN_AT   = int(os.environ.get("GROQ_QUOTA_WARN_AT", "200"))
GROQ_QUOTA_WARN_STEP = int(os.environ.get("GROQ_QUOTA_WARN_STEP", "50"))


#
# Owned here rather than by the C# client: tuning beam size, VAD, or hallucination
# suppression is then a proxy restart, not a plugin rebuild/redeploy/SDR# restart.
# All are env-overridable for A/B testing with server/bench.py without editing code.
# ---------------------------------------------------------------------------

# Fluent example transmissions, using vocabulary actually observed in Rotterdam VHF traffic.
# Measured 2026-08-06 over 244 hand-referenced clips: 3.7 points of WER better than the
# previous prompt through the deployed path (CI [-6.7%, -1.0%], sign test p=0.0008), and 4.4
# points better on a held-out set it was not derived from. See docs/design-notes.md.
#
# Contains NO vessel name, deliberately. The previous prompt named an invented vessel
# ("Motortanker Neptune") that matches a real AIS entry at 100, and was measured returning
# that name on clips where nothing of the sort was said -- a phantom vessel with a real MMSI
# attached. Any name here can be echoed into output and then matched against AIS.
DEFAULT_MARITIME_PROMPT = (
    "Maas Approach, Maas Aanloop, this is the inbound motortanker, requesting "
    "permission to enter the Botlek, over. "
    "Maas Approach, roger, proceed to VHF channel six one, out. "
    "Rotterdam VTS, be advised we are standing by on channel one six, over. "
    "Pilot Maas, we are outbound from Europoort past the Maasvlakte, our draught "
    "is eleven metres twenty, pilot ladder portside, over. "
    "Maas Approach, my intention is to proceed for East Anchorage, crossing the "
    "Deepwater route, ETA at the Maas Center buoy one four four five, over. "
    "Understood, shall we change to channel seven seven, over."
)


def _effective_prompt(client_prompt: str) -> str:
    """The prompt actually sent to the decoder for this request.

    Shared by the param builders and the echo filter, so the filter always compares against
    the prompt that was really in force rather than a copy that can drift from it.
    """
    return client_prompt or os.environ.get("WHISPER_PROMPT", DEFAULT_MARITIME_PROMPT)


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
        "prompt": _effective_prompt(client_prompt),
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


def _truncate_prompt(text: str, max_words: int = None) -> str:
    """Trim a prompt to Groq's documented length cap.

    Over-long prompts are a hard 400 from the API, which would cost a real chunk of
    radio audio. Trimming is the lesser evil -- the prompt is a decoding hint, not
    content, so losing its tail degrades nothing that matters.
    """
    limit = GROQ_PROMPT_MAX_WORDS if max_words is None else max_words
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit])


def _build_groq_fields(client_language: str, client_prompt: str) -> dict:
    """Form fields for Groq's OpenAI-compatible transcription endpoint.

    Deliberately narrower than _build_whisper_params: Groq accepts only model,
    language, prompt, temperature and response_format. The decoder tuning the local
    backend uses (beam_size=5, best_of=5, carry_initial_prompt, suppress_nst) has no
    equivalent here and is simply not available -- Groq serves the same
    whisper-large-v3 weights, but its decoding settings are not exposed.

    Shares the WHISPER_* env knobs with the local path on purpose, so one setting
    means one thing regardless of which backend is active.
    """
    return {
        "model": GROQ_MODEL,
        "temperature": os.environ.get("WHISPER_TEMPERATURE", "0"),
        "response_format": "json",
        "language": client_language or os.environ.get("WHISPER_LANGUAGE", "en"),
        "prompt": _truncate_prompt(_effective_prompt(client_prompt)),
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
# Transcription backends
#
# One seam, two implementations, selected by STT_BACKEND. Both return
# (status, body, headers) where body is the raw JSON bytes of an OpenAI-style
# {"text": ...} envelope. Everything downstream of here -- the hallucination
# filter, STT corrections, Claude vessel extraction, AIS enrichment -- reads only
# that envelope, so it is backend-agnostic and untouched by the choice made here.
#
# Errors are normalised to (503, {"error": ...}) to match what the plugin already
# renders into its transcript pane; it needs no change to work with either backend.
# ---------------------------------------------------------------------------


def _error_response(message: str) -> tuple[int, bytes, list]:
    return 503, json.dumps({"error": message}).encode("utf-8"), []


def _transcribe_whisper_cpp(file_info: dict, language: str, prompt: str) -> tuple[int, bytes, list]:
    """Transcribe via the local whisper.cpp server in WSL2."""
    params = _build_whisper_params(client_language=language, client_prompt=prompt)
    boundary, body = _build_multipart(params, file_info)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }

    # whisper.cpp's server has been observed to non-deterministically return HTTP 500
    # "failed to process audio" for the exact same request/audio that succeeds moments
    # later when VAD + beam search are both enabled — a flakiness bug in the backend,
    # not something fixable here. Since the same request often succeeds on retry, one
    # immediate retry is a cheap mitigation.
    #
    # It can also hang outright (see the watchdog above) rather than error, so every
    # attempt is tracked in _inflight_requests for the watchdog thread to see; killing
    # the backend mid-hang drops this socket and turns the exception below into a fast
    # 503 instead of a multi-minute wait.
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        req_id = _inflight_begin()
        try:
            conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=90)
            conn.request("POST", "/inference", body=body, headers=headers)
            resp         = conn.getresponse()
            resp_body    = resp.read()
            resp_headers = resp.getheaders()
            conn.close()
        except Exception as exc:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] backend error (attempt {attempt}/{max_attempts}): {exc}", flush=True)
            return _error_response(str(exc))
        finally:
            _inflight_end(req_id)

        if resp.status < 500 or attempt == max_attempts:
            return resp.status, resp_body, resp_headers

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] backend HTTP {resp.status}, retrying ({attempt}/{max_attempts})...", flush=True)


def _parse_retry_after(value: str) -> float | None:
    """Seconds from a Retry-After header, or None if absent/not a plain number.

    Groq sends a decimal seconds value. The HTTP-date form is legal but unused here,
    and treating an unparseable value as "don't wait" is the safe default.
    """
    try:
        return float(value.strip())
    except (AttributeError, TypeError, ValueError):
        return None


_quota_lock = threading.Lock()
_quota_last_bucket: int | None = None


def _check_groq_quota(headers: list) -> None:
    """Warn as the daily request allowance runs down.

    Requests/day (2,000 on the free tier) is the only Groq limit this workload can
    realistically reach — roughly 19 hours of continuous busy-channel monitoring.
    Reaching it silently would mean every subsequent chunk is lost until the quota
    resets, so it's worth surfacing early. Groq returns the counter on every
    response, so nothing has to be tracked or estimated locally.

    Warnings are bucketed rather than per-request: one line each time another
    GROQ_QUOTA_WARN_STEP requests are consumed, not one line per chunk.
    """
    global _quota_last_bucket

    remaining = None
    for key, value in headers:
        if key.lower() == "x-ratelimit-remaining-requests":
            try:
                remaining = int(value)
            except (TypeError, ValueError):
                return
            break
    if remaining is None:
        return

    with _quota_lock:
        if remaining > GROQ_QUOTA_WARN_AT:
            # Comfortably clear, or the daily quota has just rolled over — re-arm so
            # tomorrow's run warns again instead of staying silent below yesterday's mark.
            _quota_last_bucket = None
            return
        bucket = remaining // max(GROQ_QUOTA_WARN_STEP, 1)
        if _quota_last_bucket is not None and bucket >= _quota_last_bucket:
            return
        _quota_last_bucket = bucket

    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [quota] Groq daily requests remaining: {remaining}", flush=True)


def _transcribe_groq(file_info: dict, language: str, prompt: str) -> tuple[int, bytes, list]:
    """Transcribe via Groq's hosted Whisper API."""
    if not GROQ_API_KEY:
        return _error_response("GROQ_API_KEY not set (needed when STT_BACKEND=groq)")

    fields = _build_groq_fields(client_language=language, client_prompt=prompt)
    boundary, body = _build_multipart(fields, file_info)
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }

    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            conn = http.client.HTTPSConnection(GROQ_HOST, timeout=GROQ_TIMEOUT_S)
            conn.request("POST", GROQ_PATH, body=body, headers=headers)
            resp         = conn.getresponse()
            resp_body    = resp.read()
            resp_headers = resp.getheaders()
            retry_after  = resp.getheader("Retry-After", "")
            conn.close()
        except Exception as exc:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] groq error (attempt {attempt}/{max_attempts}): {exc}", flush=True)
            return _error_response(str(exc))

        _check_groq_quota(resp_headers)

        if resp.status < 500 and resp.status != 429:
            return resp.status, resp_body, resp_headers
        if attempt == max_attempts:
            return resp.status, resp_body, resp_headers

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        if resp.status == 429:
            # Only worth waiting out a rate limit if it clears quickly. The plugin sends
            # chunks one at a time, so sleeping here stalls everything queued behind this
            # one; past a few seconds it's better to drop this chunk and let the next
            # start clean than to build a backlog the send queue will discard anyway.
            delay = _parse_retry_after(retry_after)
            if delay is None or delay > GROQ_MAX_RETRY_WAIT_S:
                print(f"[{ts}] groq rate limited (retry-after={retry_after or 'n/a'}), giving up on this chunk", flush=True)
                return resp.status, resp_body, resp_headers
            print(f"[{ts}] groq rate limited, waiting {delay:.1f}s then retrying...", flush=True)
            time.sleep(delay)
        else:
            print(f"[{ts}] groq HTTP {resp.status}, retrying ({attempt}/{max_attempts})...", flush=True)


# Headers that must not be relayed from the upstream response to the plugin.
#
# The critical one is `connection`. Groq sits behind Cloudflare and answers
# "Connection: keep-alive"; BaseHTTPRequestHandler.send_header() *special-cases* that
# value and sets close_connection = False, so the proxy then never closes the socket.
# WhisperClient.ReadToEndAsync() reads until EOF and ignores Content-Length, so the
# plugin sits on a fully-delivered response until its own 60s cancel fires. whisper.cpp
# never triggered this because cpp-httplib honours the client's "Connection: close".
#
# The rest are hop-by-hop headers (RFC 7230 6.1), framing headers invalidated by the
# post-processing rewrite, headers send_response() emits itself (duplicated otherwise),
# and CDN bookkeeping -- a Cloudflare session cookie means nothing to an SDR# plugin.
_SKIP_RESPONSE_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
    "content-length", "content-encoding",
    "date", "server",
    "set-cookie", "alt-svc", "cf-ray", "cf-cache-status", "cache-control",
    "vary", "via", "strict-transport-security",
})


def _client_response_headers(upstream: list) -> list:
    """Filter an upstream response's headers down to what the plugin should receive."""
    return [(k, v) for k, v in upstream if k.lower() not in _SKIP_RESPONSE_HEADERS]


def transcribe(file_info: dict, language: str, prompt: str) -> tuple[int, bytes, list]:
    """Transcribe one audio chunk using whichever backend STT_BACKEND selects."""
    if STT_BACKEND == "groq":
        return _transcribe_groq(file_info, language, prompt)
    return _transcribe_whisper_cpp(file_info, language, prompt)
