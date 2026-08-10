"""One call signature over several providers.

The conversation-correction pass has to be scored across models before one is chosen, and the
2026-08-03 bake-off found free endpoints are a supply problem before a quality one. Putting the
provider behind a signature means the bake-off sweeps configuration rather than code.

claude.py stays as it is: it owns the live path's client, whose 15s timeout is tuned for a call
that blocks a transcription response. This module's callers are off that path.
"""

import json
import os
import re
import urllib.request

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(Exception):
    """Any provider failure, so callers catch one type rather than three SDKs' worth."""


def strip_code_fence(text: str) -> str:
    """The JSON inside a markdown fence, or the text unchanged."""
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _anthropic_client(timeout_s: float):
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=1)


def _complete_anthropic(system, user, *, model, temperature, timeout_s, max_tokens):
    client = _anthropic_client(timeout_s)
    message = client.messages.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        system=system, messages=[{"role": "user", "content": user}],
    )
    return message.content[0].text.strip()


def _complete_openrouter(system, user, *, model, temperature, timeout_s, max_tokens):
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise LLMError("OPENROUTER_API_KEY is not set")
    payload = json.dumps({
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }).encode("utf-8")
    request = urllib.request.Request(OPENROUTER_URL, data=payload, method="POST")
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Content-Type", "application/json")
    # A custom User-Agent is not optional: Cloudflare 403s the default Python-urllib one,
    # which reads as a model-specific failure and is not.
    request.add_header("User-Agent", "sdrsharp-stt-proxy/1.0")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"].strip()


_PROVIDERS = {"anthropic": _complete_anthropic, "openrouter": _complete_openrouter}


def complete(system: str, user: str, *, provider: str, model: str,
             temperature: float = 0.0, timeout_s: float = 60.0,
             max_tokens: int = 4096) -> str:
    """One completion. Raises LLMError for every failure mode, including a bad provider."""
    fn = _PROVIDERS.get(provider)
    if fn is None:
        raise LLMError(f"unknown provider {provider!r}; known: {sorted(_PROVIDERS)}")
    try:
        return fn(system, user, model=model, temperature=temperature,
                  timeout_s=timeout_s, max_tokens=max_tokens)
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(str(exc)) from exc
