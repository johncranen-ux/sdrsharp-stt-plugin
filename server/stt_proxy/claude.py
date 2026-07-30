"""Shared Anthropic client.

Its own module so identification and conversation resolution can share one client without
importing each other. Created lazily: an unset ANTHROPIC_API_KEY disables the maritime
features rather than being an error at startup.
"""

import os

import anthropic


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
