"""Tests for whisper-proxy.py: hallucination filtering, STT corrections, and the
multipart parse/rebuild that lets the proxy own the whisper.cpp decoder parameters.

Run with: py -m pytest server/tests -v
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "whisper-proxy.py"


def _load_proxy_module():
    # whisper-proxy.py has a hyphen in its name, so it can't be `import`ed normally.
    spec = importlib.util.spec_from_file_location("whisper_proxy", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["whisper_proxy"] = module
    spec.loader.exec_module(module)
    return module


proxy = _load_proxy_module()


# ---------------------------------------------------------------------------
# Hallucination filter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "", " ", ".", "...", "!?",
    "you", "You.", "thank you", "Thank you for watching",
    "please subscribe", "bye", "goodbye",
    "the the the the",
])
def test_is_hallucination_true(text):
    assert proxy._is_hallucination(text) is True


@pytest.mark.parametrize("text", [
    "Maas Approach, this is Motortanker Neptune, over",
    "Roger, copy",
    "Standing by on channel one six",
    "you are cleared to enter the Botlek",  # contains "you" but isn't just "you"
])
def test_is_hallucination_false(text):
    assert proxy._is_hallucination(text) is False


# ---------------------------------------------------------------------------
# STT corrections
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_substring", [
    ("mass approach, over", "Maas Approach"),
    ("march approach, over", "Maas Approach"),
    ("this is mass control", "Maas control"),
    ("what is your cosine", "Callsign"),
    ("what is your call sign", "Callsign"),
    ("motor tanker Neptune", "Motortanker Neptune"),
    ("draft twelve metres", "draught twelve metres"),
    ("watch out for the boys", "watch out for the buoys"),
    ("mars approach, over", "Maas Approach"),
    ("this is mars control", "Maas control"),
    ("watch out for the boy", "watch out for the buoy"),
])
def test_apply_sttt_corrections(raw, expected_substring):
    result = proxy._apply_sttt_corrections(raw)
    assert expected_substring in result


# ---------------------------------------------------------------------------
# Multipart parse / rebuild round-trip
# ---------------------------------------------------------------------------

def _build_client_style_multipart(fields: dict, file_bytes: bytes) -> tuple[str, bytes]:
    """Mimics WhisperClient.cs's BuildMultipartBody: field parts, then a file part."""
    boundary = "----TestBoundary12345"
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        f'Content-Type: audio/wav\r\n\r\n'.encode()
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def test_parse_multipart_extracts_fields_and_file():
    content_type, body = _build_client_style_multipart(
        {"temperature": "0", "language": "en", "prompt": "hello there"},
        b"RIFF....fake wav bytes....",
    )

    fields, file_info = proxy._parse_multipart(content_type, body)

    assert fields["temperature"] == "0"
    assert fields["language"] == "en"
    assert fields["prompt"] == "hello there"
    assert file_info is not None
    assert file_info["filename"] == "audio.wav"
    assert file_info["data"] == b"RIFF....fake wav bytes...."


def test_parse_multipart_no_boundary_raises():
    with pytest.raises(ValueError):
        proxy._parse_multipart("multipart/form-data", b"garbage")


def test_build_multipart_round_trips_through_parse():
    file_info = {"field": "file", "filename": "audio.wav", "content_type": "audio/wav", "data": b"\x01\x02\x03\x04"}
    boundary, body = proxy._build_multipart({"beam_size": "5", "vad": "true"}, file_info)

    fields, parsed_file = proxy._parse_multipart(f"multipart/form-data; boundary={boundary}", body)

    assert fields["beam_size"] == "5"
    assert fields["vad"] == "true"
    assert parsed_file["data"] == b"\x01\x02\x03\x04"
    assert parsed_file["filename"] == "audio.wav"


# ---------------------------------------------------------------------------
# Whisper params
# ---------------------------------------------------------------------------

def test_build_whisper_params_uses_defaults_when_client_omits():
    params = proxy._build_whisper_params(client_language="", client_prompt="")
    assert params["language"] == "en"
    assert params["prompt"] == proxy.DEFAULT_MARITIME_PROMPT
    assert params["beam_size"] == "5"
    # Off by default per server/bench.py results on real captures: VAD-on configs did not
    # outperform the equivalent VAD-off config, and whisper.cpp's VAD+beam combination has
    # its own flakiness bugs (see whisper-proxy.py comment at _build_whisper_params).
    assert params["vad"] == "false"


def test_build_whisper_params_honors_client_overrides():
    params = proxy._build_whisper_params(client_language="fr", client_prompt="custom prompt text")
    assert params["language"] == "fr"
    assert params["prompt"] == "custom prompt text"
    # Decoder tuning params are never client-controlled, even when overrides are given.
    assert params["beam_size"] == "5"


def test_env_bool_accepts_common_truthy_values(monkeypatch):
    for value in ("1", "true", "True", "yes"):
        monkeypatch.setenv("TEST_FLAG", value)
        assert proxy._env_bool("TEST_FLAG", "false") == "true"

    for value in ("0", "false", "no", ""):
        monkeypatch.setenv("TEST_FLAG", value)
        assert proxy._env_bool("TEST_FLAG", "true") == "false"


# ---------------------------------------------------------------------------
# Groq params
# ---------------------------------------------------------------------------

def test_build_groq_fields_uses_defaults_when_client_omits():
    fields = proxy._build_groq_fields(client_language="", client_prompt="")
    assert fields["language"] == "en"
    assert fields["prompt"] == proxy.DEFAULT_MARITIME_PROMPT
    assert fields["temperature"] == "0"
    assert fields["response_format"] == "json"
    assert fields["model"] == proxy.GROQ_MODEL


def test_build_groq_fields_honors_client_overrides():
    fields = proxy._build_groq_fields(client_language="nl", client_prompt="custom prompt text")
    assert fields["language"] == "nl"
    assert fields["prompt"] == "custom prompt text"


def test_build_groq_fields_omits_params_groq_rejects():
    """Groq's endpoint 400s on unknown fields, and has no equivalent for whisper.cpp's
    decoder tuning. Sending them would fail every chunk."""
    fields = proxy._build_groq_fields(client_language="", client_prompt="")
    for unsupported in ("beam_size", "best_of", "carry_initial_prompt", "suppress_nst", "vad"):
        assert unsupported not in fields


def test_truncate_prompt_leaves_short_prompts_untouched():
    text = "Maas Approach, this is Motortanker Neptune, over."
    assert proxy._truncate_prompt(text) == text
    # The shipped default must not be silently trimmed.
    assert proxy._truncate_prompt(proxy.DEFAULT_MARITIME_PROMPT) == proxy.DEFAULT_MARITIME_PROMPT


def test_truncate_prompt_caps_overlong_prompts():
    long_prompt = " ".join(f"word{i}" for i in range(500))
    result = proxy._truncate_prompt(long_prompt, max_words=140)
    assert len(result.split()) == 140
    assert result.startswith("word0 word1")


def test_build_groq_fields_truncates_a_long_client_prompt():
    fields = proxy._build_groq_fields(
        client_language="", client_prompt=" ".join(["padding"] * 400)
    )
    assert len(fields["prompt"].split()) == proxy.GROQ_PROMPT_MAX_WORDS


def test_groq_fields_round_trip_through_multipart():
    file_info = {"field": "file", "filename": "audio.wav", "content_type": "audio/wav", "data": b"\x01\x02"}
    fields = proxy._build_groq_fields(client_language="en", client_prompt="")
    boundary, body = proxy._build_multipart(fields, file_info)

    parsed, parsed_file = proxy._parse_multipart(f"multipart/form-data; boundary={boundary}", body)

    assert parsed["model"] == proxy.GROQ_MODEL
    assert parsed["language"] == "en"
    assert parsed_file["data"] == b"\x01\x02"
    assert parsed_file["filename"] == "audio.wav"


@pytest.mark.parametrize("raw,expected", [
    ("2", 2.0), ("7.66", 7.66), (" 3 ", 3.0),
    ("", None), ("Wed, 21 Oct 2015 07:28:00 GMT", None), (None, None),
])
def test_parse_retry_after(raw, expected):
    assert proxy._parse_retry_after(raw) == expected


# ---------------------------------------------------------------------------
# Backend dispatch and the Groq transport
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = body
        self._headers = headers or {}

    def read(self):
        return self._body

    def getheaders(self):
        return list(self._headers.items())

    def getheader(self, name, default=None):
        return self._headers.get(name, default)


class _FakeConnection:
    """Stands in for http.client.HTTPSConnection; records what was sent."""
    instances = []

    def __init__(self, host, timeout=None):
        self.host = host
        self.timeout = timeout
        self.requests = []
        _FakeConnection.instances.append(self)

    def request(self, method, path, body=None, headers=None):
        self.requests.append({"method": method, "path": path, "body": body, "headers": headers or {}})

    def getresponse(self):
        return _FakeConnection.responses.pop(0)

    def close(self):
        pass


@pytest.fixture
def fake_groq(monkeypatch):
    _FakeConnection.instances = []
    _FakeConnection.responses = []
    monkeypatch.setattr(proxy.http.client, "HTTPSConnection", _FakeConnection)
    monkeypatch.setattr(proxy, "GROQ_API_KEY", "gsk_test_key")
    return _FakeConnection


_FILE_INFO = {"field": "file", "filename": "audio.wav", "content_type": "audio/wav", "data": b"RIFFfake"}


def test_transcribe_dispatches_to_groq_when_selected(monkeypatch):
    monkeypatch.setattr(proxy, "STT_BACKEND", "groq")
    monkeypatch.setattr(proxy, "_transcribe_groq", lambda *a, **k: (200, b'{"text":"groq"}', []))
    monkeypatch.setattr(proxy, "_transcribe_whisper_cpp", lambda *a, **k: pytest.fail("wrong backend"))

    status, body, _ = proxy.transcribe(_FILE_INFO, language="en", prompt="")
    assert (status, body) == (200, b'{"text":"groq"}')


def test_transcribe_dispatches_to_whisper_cpp_when_selected(monkeypatch):
    monkeypatch.setattr(proxy, "STT_BACKEND", "whisper_cpp")
    monkeypatch.setattr(proxy, "_transcribe_whisper_cpp", lambda *a, **k: (200, b'{"text":"local"}', []))
    monkeypatch.setattr(proxy, "_transcribe_groq", lambda *a, **k: pytest.fail("wrong backend"))

    status, body, _ = proxy.transcribe(_FILE_INFO, language="en", prompt="")
    assert (status, body) == (200, b'{"text":"local"}')


def test_transcribe_groq_missing_key_returns_error_envelope(monkeypatch):
    monkeypatch.setattr(proxy, "GROQ_API_KEY", "")
    status, body, _ = proxy._transcribe_groq(_FILE_INFO, language="en", prompt="")
    assert status == 503
    assert "GROQ_API_KEY" in json.loads(body)["error"]


def test_transcribe_groq_success_sends_expected_request(fake_groq):
    fake_groq.responses = [_FakeResponse(200, b'{"text":"Maas Approach, over"}')]

    status, body, _ = proxy._transcribe_groq(_FILE_INFO, language="en", prompt="")

    assert status == 200
    assert json.loads(body)["text"] == "Maas Approach, over"

    sent = fake_groq.instances[0].requests[0]
    assert fake_groq.instances[0].host == proxy.GROQ_HOST
    assert sent["path"] == proxy.GROQ_PATH
    assert sent["headers"]["Authorization"] == "Bearer gsk_test_key"
    assert b"RIFFfake" in sent["body"]
    assert proxy.GROQ_MODEL.encode() in sent["body"]


def test_transcribe_groq_transport_failure_returns_503(fake_groq, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(proxy.http.client, "HTTPSConnection", boom)

    status, body, _ = proxy._transcribe_groq(_FILE_INFO, language="en", prompt="")
    assert status == 503
    assert "connection reset" in json.loads(body)["error"]


def test_transcribe_groq_retries_once_on_server_error(fake_groq):
    fake_groq.responses = [
        _FakeResponse(500, b'{"error":"upstream"}'),
        _FakeResponse(200, b'{"text":"recovered"}'),
    ]

    status, body, _ = proxy._transcribe_groq(_FILE_INFO, language="en", prompt="")

    assert status == 200
    assert json.loads(body)["text"] == "recovered"
    assert len(fake_groq.instances) == 2


def test_transcribe_groq_waits_out_a_short_rate_limit(fake_groq, monkeypatch):
    slept = []
    monkeypatch.setattr(proxy.time, "sleep", slept.append)
    fake_groq.responses = [
        _FakeResponse(429, b'{"error":"rate limited"}', {"Retry-After": "1.5"}),
        _FakeResponse(200, b'{"text":"after wait"}'),
    ]

    status, body, _ = proxy._transcribe_groq(_FILE_INFO, language="en", prompt="")

    assert status == 200
    assert json.loads(body)["text"] == "after wait"
    assert slept == [1.5]


def test_transcribe_groq_gives_up_on_a_long_rate_limit(fake_groq, monkeypatch):
    """The plugin sends chunks serially, so a long sleep here stalls every chunk behind
    this one. Surfacing the 429 lets the next chunk start clean instead."""
    slept = []
    monkeypatch.setattr(proxy.time, "sleep", slept.append)
    fake_groq.responses = [_FakeResponse(429, b'{"error":"rate limited"}', {"Retry-After": "60"})]

    status, _, _ = proxy._transcribe_groq(_FILE_INFO, language="en", prompt="")

    assert status == 429
    assert slept == []
    assert len(fake_groq.instances) == 1
