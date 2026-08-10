"""Tests for llm.py: one signature over several providers."""

import io
import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import llm  # noqa: E402


class _FakeMessages:
    def __init__(self, reply, recorder):
        self._reply, self._recorder = reply, recorder

    def create(self, **kwargs):
        self._recorder.update(kwargs)
        return type("R", (), {"content": [type("C", (), {"text": self._reply})()]})()


class _FakeClient:
    def __init__(self, reply, recorder):
        self.messages = _FakeMessages(reply, recorder)


def test_anthropic_returns_the_reply_text(monkeypatch):
    calls = {}
    monkeypatch.setattr(llm, "_anthropic_client",
                        lambda timeout_s: _FakeClient('{"ok": true}', calls))
    assert llm.complete("sys", "usr", provider="anthropic", model="m") == '{"ok": true}'


def test_temperature_is_pinned_to_zero_by_default(monkeypatch):
    """Sampling noise made a previous A/B unmeasurable; nothing here wants sampling."""
    calls = {}
    monkeypatch.setattr(llm, "_anthropic_client",
                        lambda timeout_s: _FakeClient("x", calls))
    llm.complete("sys", "usr", provider="anthropic", model="m")
    assert calls["temperature"] == 0


def test_an_unknown_provider_is_an_error():
    with pytest.raises(llm.LLMError, match="unknown provider"):
        llm.complete("sys", "usr", provider="nope", model="m")


def test_a_provider_failure_is_wrapped_as_llm_error(monkeypatch):
    def boom(timeout_s):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(llm, "_anthropic_client", boom)
    with pytest.raises(llm.LLMError, match="connection reset"):
        llm.complete("sys", "usr", provider="anthropic", model="m")


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', '{"a": 1}'),
    ('```json\n{"a": 1}\n```', '{"a": 1}'),
    ('```\n{"a": 1}\n```', '{"a": 1}'),
    ('here you go:\n```json\n{"a": 1}\n```\n', '{"a": 1}'),
])
def test_code_fences_are_stripped(raw, expected):
    """Models wrap JSON in fences regardless of instructions; the resolver already
    works around this and the new pass must not repeat the workaround."""
    assert llm.strip_code_fence(raw) == expected


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_openrouter_sends_system_and_user_and_returns_content(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _FakeResponse(json.dumps(
            {"choices": [{"message": {"content": '{"ok": 1}'}}]}).encode("utf-8"))

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    out = llm.complete("sys", "usr", provider="openrouter", model="free/model",
                       timeout_s=12.0)
    assert out == '{"ok": 1}'
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["messages"][1] == {"role": "user", "content": "usr"}
    assert seen["body"]["temperature"] == 0.0
    assert seen["timeout"] == 12.0


def test_openrouter_sets_a_custom_user_agent(monkeypatch):
    """Cloudflare 403s the default Python-urllib agent (error 1010), which reads as a
    model-specific fault and is not. Cost real time once already."""
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return _FakeResponse(json.dumps(
            {"choices": [{"message": {"content": "x"}}]}).encode("utf-8"))

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    llm.complete("s", "u", provider="openrouter", model="m")
    assert "python-urllib" not in seen["user-agent"].lower()


def test_openrouter_without_a_key_is_an_llm_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="OPENROUTER_API_KEY"):
        llm.complete("s", "u", provider="openrouter", model="m")
