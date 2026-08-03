#!/usr/bin/env python3
"""Score LLM transcript-correction passes against the hand-transcribed reference set.

This is the cheap A/B described in docs/design-notes.md: rather than re-transcribing
audio, it re-scores hypotheses already captured by bench.py. No GPU, no SDR#, no audio.

Each contender receives one raw STT hypothesis and returns a corrected one; the result is
scored with bench.word_error_counts and pooled across clips (total edits / total reference
words), the same metric every other measurement in this project uses.

The regex pass that runs in production today is a contender like any other, so the question
"does an LLM beat the list we already have" is answered directly rather than by comparing
two numbers taken under different conditions.

Usage:
    py bench_correct.py --contenders all
    py bench_correct.py --contenders regex,gemma-31b --limit 5   # smoke test

Responses are cached under bench-correct-cache/ keyed by (contender, prompt, clip), so a
re-run costs nothing and an interrupted run resumes. Delete the directory to force refetch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench  # noqa: E402  (path set above)
from stt_proxy.corrections import _apply_sttt_corrections  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "bench-correct-cache"


# ---------------------------------------------------------------------------
# The correction prompt
#
# One prompt for every model, so what is measured is the model and not the prompt. It
# carries the same domain knowledge the regex list encodes -- Rotterdam place names,
# procedure words, the phonetic alphabet -- because withholding it would measure how much
# maritime VHF each model happens to have memorised, which is not the question.
#
# The instructions are mostly restrictions. The failure this pipeline weighs most heavily
# is not an uncorrected error but an invented one: the 2026-07-30 work found that a model
# given latitude over transcript text writes plausible radio traffic that was never said.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You correct speech-to-text transcripts of maritime VHF radio traffic on channel 1 \
(160.650 MHz) near the Port of Rotterdam, the Netherlands.

The audio is noisy shortwave-quality FM. Speakers are mostly non-native English speakers \
(Dutch, Greek, Filipino, Russian, Indian, Turkish). The transcript you receive was produced \
by Whisper and contains recognition errors, especially in proper nouns.

Correct only what is clearly a mis-recognition of:
- Rotterdam-area names: Maas Approach, Maas Center, Rotterdam VTS, Pilot Rotterdam, \
Botlek, Europoort, Maasvlakte, Steenbank, Hoek van Holland, Caland, Beneluxhaven, \
Scheveningen, Recon buoy, Echo 1 / Echo 3 buoys, Deepwater Route.
- Standard radio procedure: over, out, roger, wilco, standing by, channel one six, \
stand by, copy, understood, say again, this is.
- NATO phonetic alphabet: Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliet \
Kilo Lima Mike November Oscar Papa Quebec Romeo Sierra Tango Uniform Victor Whiskey X-ray \
Yankee Zulu.
- Maritime vocabulary: draught, buoy, anchor, heave up, pilot ladder, starboard, port \
side, inbound, outbound, southbound, northbound, motor vessel, motortanker, callsign, \
ETA, pilot boarding ground.
- Vessel-name spelling, when the intended name is obvious from context.

Rules, in order of importance:
1. Never invent content. Do not add words, sentences, greetings or replies that are not \
already in the input. Do not continue the conversation.
2. Never remove content. Every utterance in the input must survive, even if garbled.
3. Make the smallest edit that fixes a clear error. If a word is merely unusual, or you \
are unsure what was meant, leave it exactly as it is.
4. Keep the speaker's own words, word order, grammar and disfluencies. You are not \
rewriting or improving the English.
5. Numbers stay in the form they were transcribed (do not convert "one eight zero zero" \
to "1800", or the reverse).
6. Channel numbers on this frequency are "zero one" (channel 01, the Maas Approach working \
channel) and "one six" (channel 16, the calling channel). Vessels are routinely told to \
stand by on both, and say so as "zero one, one six" or "one and one six". This is correct \
as transcribed. NEVER rewrite it to "channel one six", never drop the "zero one", and never \
insert the word "channel" where it was not spoken.

The transcript to correct is given between <transcript> tags. It may be a single word, a \
fragment, or badly garbled -- correct it as given and return it anyway. Never ask for a \
transcript, never reply conversationally, never comment on the input.

Reply with the corrected transcript and nothing else: no preamble, no explanation, no \
quotation marks, no formatting, no tags."""

USER_TEMPLATE = "<transcript>\n{text}\n</transcript>"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class RateLimiter:
    """Minimum spacing between calls, shared by every contender on one provider.

    OpenRouter's 20 requests/minute is an account-wide ceiling across free models, so
    three contenders running concurrently must share one limiter or they will trip it.
    """

    def __init__(self, per_minute: float):
        self._interval = 60.0 / per_minute
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if sleep_for:
            time.sleep(sleep_for)


# OpenRouter's own ceiling is 20/min, but the :free endpoints are shared upstream capacity
# and the provider behind them rate-limits independently: at 15/min every call came back
# "temporarily rate-limited upstream" from Google AI Studio. 6/min is what actually gets
# through. This is a property of free endpoints, not of the models, and is itself a finding.
LIMITERS = {
    "openrouter": RateLimiter(6),
    "groq":       RateLimiter(25),   # ceiling is 30/min
    "anthropic":  RateLimiter(50),
}


def _post_json(url: str, payload: dict, headers: dict, timeout: float = 180.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          # Groq sits behind Cloudflare, which answers the
                                          # default "Python-urllib/3.x" with a 403 (error
                                          # 1010) before the request ever reaches the API.
                                          "User-Agent": "sdrsharp-stt-bench/1.0",
                                          **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _call_with_retries(fn: Callable[[], dict], attempts: int = 5) -> dict:
    """Retry on rate limits and transient upstream failures.

    Free endpoints are shared capacity and return 429 under load often enough that a run
    without this finishes with holes in it, which would silently bias the pooled WER
    towards whichever clips happened to succeed. Backoff runs out past two minutes because
    upstream saturation lasts that long; being slow is much cheaper here than being partial.
    """
    delay = 5.0
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            last = RuntimeError(f"HTTP {exc.code}: {detail}")
            if exc.code not in (408, 429, 500, 502, 503, 504):
                raise last from exc
        except Exception as exc:  # noqa: BLE001 - network layer, anything can surface
            last = exc
        if attempt < attempts - 1:
            time.sleep(delay)
            delay *= 2
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Contenders
# ---------------------------------------------------------------------------

def _openai_compatible(provider: str, base_url: str, key_env: str, model: str,
                       text: str) -> tuple[str, dict]:
    key = os.environ.get(key_env, "").strip()
    if not key:
        raise RuntimeError(f"{key_env} is not set")
    payload = {
        "model": model,
        "temperature": 0,
        # Generous, because the Nemotron models reason before answering and the reasoning
        # is drawn from the same budget. Truncating mid-reasoning does not yield a short
        # answer, it yields raw chain-of-thought in the content field -- which would be
        # scored as a wildly hallucinated transcript and blamed on the model's judgement
        # rather than on this setting.
        "max_tokens": 3000,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(text=text)},
        ],
    }
    if provider == "openrouter":
        payload["reasoning"] = {"exclude": True}  # OpenRouter-specific; Groq rejects it
    LIMITERS[provider].wait()
    data = _call_with_retries(lambda: _post_json(base_url, payload, {"Authorization": f"Bearer {key}"}))
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"no choices in response: {json.dumps(data)[:300]}")
    return choices[0]["message"]["content"] or "", data.get("usage") or {}


def openrouter(model: str) -> Callable[[str], tuple[str, dict]]:
    def run(text: str) -> tuple[str, dict]:
        return _openai_compatible("openrouter", "https://openrouter.ai/api/v1/chat/completions",
                                  "OPENROUTER_API_KEY", model, text)
    return run


def groq(model: str) -> Callable[[str], tuple[str, dict]]:
    def run(text: str) -> tuple[str, dict]:
        return _openai_compatible("groq", "https://api.groq.com/openai/v1/chat/completions",
                                  "GROQ_API_KEY", model, text)
    return run


def anthropic(model: str) -> Callable[[str], tuple[str, dict]]:
    def run(text: str) -> tuple[str, dict]:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        payload = {
            "model": model,
            "max_tokens": 1200,
            "temperature": 0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": USER_TEMPLATE.format(text=text)}],
        }
        LIMITERS["anthropic"].wait()
        data = _call_with_retries(lambda: _post_json(
            "https://api.anthropic.com/v1/messages", payload,
            {"x-api-key": key, "anthropic-version": "2023-06-01"}))
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts), data.get("usage") or {}
    return run


def _extract_vessel_text(text: str) -> str:
    """The transcript production's channel-01 path would produce for this transmission.

    Calls the real extract_vessel, so the prompt, the callsign guard and the regex pass it
    applies to Claude's output are all the shipped ones rather than a reconstruction. The
    AIS cache is empty here, so no [AIS: ...] hints are offered -- hints only ever affect
    name spelling, and the alternative would be scoring against whichever ships happened to
    be in the estuary today.
    """
    from stt_proxy.identify import extract_vessel

    LIMITERS["anthropic"].wait()
    return extract_vessel(text).get("text") or text


CONTENDERS: dict[str, dict[str, Any]] = {
    "regex": {
        "kind": "local",
        "label": "production regex list",
        "fn": lambda text: (_apply_sttt_corrections(text), {}),
    },
    "gemma-31b": {
        "kind": "llm", "provider": "openrouter",
        "label": "google/gemma-4-31b-it:free",
        "fn": openrouter("google/gemma-4-31b-it:free"),
    },
    "nemotron-120b": {
        "kind": "llm", "provider": "openrouter",
        "label": "nvidia/nemotron-3-super-120b-a12b:free",
        "fn": openrouter("nvidia/nemotron-3-super-120b-a12b:free"),
    },
    "gemma-26b": {
        "kind": "llm", "provider": "openrouter",
        "label": "google/gemma-4-26b-a4b-it:free",
        "fn": openrouter("google/gemma-4-26b-a4b-it:free"),
    },
    "nemotron-550b": {
        "kind": "llm", "provider": "openrouter",
        "label": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "fn": openrouter("nvidia/nemotron-3-ultra-550b-a55b:free"),
    },
    "gpt-oss-120b": {
        "kind": "llm", "provider": "groq",
        "label": "groq/openai/gpt-oss-120b",
        "fn": groq("openai/gpt-oss-120b"),
    },
    "haiku-4.5": {
        "kind": "llm", "provider": "anthropic",
        "label": "claude-haiku-4-5",
        "fn": anthropic("claude-haiku-4-5-20251001"),
    },
    # What channel 01 actually runs today. extract_vessel's prompt is a vessel-identification
    # prompt that happens to return a rewritten "text", and identify.py then applies the regex
    # list to that. Measuring it answers the question the other rows cannot: is the correction
    # already running in production any good?
    "prod-ch01": {
        "kind": "llm", "provider": "anthropic",
        "label": "production CH01 (extract_vessel -> regex)",
        "fn": lambda text: (_extract_vessel_text(text), {}),
    },
}

# Chains. The base model's replies are already cached, so each of these costs nothing:
# only the local regex pass differs.
for _base in ("haiku-4.5", "gpt-oss-120b", "gemma-26b", "nemotron-120b", "nemotron-550b"):
    CONTENDERS[f"{_base}+regex"] = {
        **CONTENDERS[_base],
        "label": CONTENDERS[_base]["label"] + " -> regex",
        "cache_name": _base,
        "post": _apply_sttt_corrections,
    }


# ---------------------------------------------------------------------------
# Output cleaning
#
# Models wrap their answer despite being told not to. Stripping a fence or a "Corrected
# transcript:" label is fair -- the wrapper is not a transcription error and scoring it as
# one would measure formatting compliance rather than correction quality. What is NOT
# stripped is added or removed content, which is the failure mode being watched for.
# ---------------------------------------------------------------------------

_THINK_RE  = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE  = re.compile(r"^```[a-zA-Z]*\n(.*?)\n?```$", re.DOTALL)
_LABEL_RE  = re.compile(r"^\s*(corrected\s+transcript|corrected|output|transcript)\s*:\s*",
                        re.IGNORECASE)


# A reply that is not a transcript at all. The Nemotron free endpoints ignore
# reasoning.exclude on a minority of clips and return chain-of-thought, sometimes
# collapsing into thousands of <unk> tokens. Pooling that into WER is meaningless -- one
# 14,000-character reply against a ten-word reference contributes more insertions than the
# whole corpus has words -- so these are counted as malformed and scored apart from the
# clips where the model actually answered.
_COT_RE = re.compile(r"^\s*(we need to|the user|okay,? (so|let)|first,? |i (should|need))",
                     re.IGNORECASE)


def is_malformed(raw: str, output: str, hypothesis: str) -> bool:
    if "<unk>" in (raw or ""):
        return True
    if _COT_RE.match(output or ""):
        return True
    # Runaway generation: a correction pass cannot legitimately triple the transcript.
    hyp_words = len(bench._normalize(hypothesis))
    return len(bench._normalize(output or "")) > hyp_words * 3 + 10


def clean_output(raw: str) -> str:
    text = _THINK_RE.sub("", raw or "").strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    text = _LABEL_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return text


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def load_clips(results_path: Path, config: str) -> list[dict]:
    data = json.loads(results_path.read_text(encoding="utf-8"))
    rows = data["results"][config]
    return [
        {"clip_id": r["clip_id"], "hypothesis": r["text"], "reference": r["reference"]}
        for r in rows
        if (r.get("reference") or "").strip() and (r.get("text") or "").strip()
    ]


DATASET = "unset"


def _cache_path(name: str, clip_id: str) -> Path:
    # Keyed on both prompt halves, so editing either starts a fresh cache rather than
    # silently mixing replies to two different questions -- and leaves the previous run's
    # replies on disk, which is what makes a prompt change a measurable A/B.
    #
    # DATASET is part of the key because clip ids restart at 0000 in every captures
    # directory: 2026-07-27's clip 0003 and 2026-07-28's clip 0003 are different audio with
    # the same name, and without this the second dataset would silently score the first
    # one's cached replies.
    stamp = hashlib.sha256((SYSTEM_PROMPT + USER_TEMPLATE).encode("utf-8")).hexdigest()[:8]
    return CACHE_DIR / f"{name}-{stamp}-{DATASET}" / f"{clip_id}.json"


def _cache_name(name: str, spec: dict) -> str:
    """Which cache a contender reads.

    Chained contenders ("haiku-4.5+regex") reuse the base model's cached replies and differ
    only in local post-processing, so measuring a chain costs no API calls at all.
    """
    return spec.get("cache_name", name)


def run_contender(name: str, spec: dict, clips: list[dict], workers: int) -> list[dict]:
    post = spec.get("post")

    def one(clip: dict) -> dict:
        cache = _cache_path(_cache_name(name, spec), clip["clip_id"])
        if cache.exists():
            record = json.loads(cache.read_text(encoding="utf-8"))
            record["cached"] = True
            if post and record.get("output"):
                record = dict(record, output=post(record["output"]))
            return record

        started = time.monotonic()
        try:
            raw, usage = spec["fn"](clip["hypothesis"])
            record = {
                "clip_id": clip["clip_id"],
                "raw": raw,
                "output": clean_output(raw),
                "elapsed": time.monotonic() - started,
                "usage": usage,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - recorded, not raised, so one clip cannot void a run
            # Deliberately not cached: a failure is a fact about the moment, not about the
            # clip, and caching it would make every later run inherit a hole it can never
            # fill. Re-running the command retries exactly the clips that failed.
            return {
                "clip_id": clip["clip_id"], "raw": "", "output": "", "cached": False,
                "elapsed": time.monotonic() - started, "usage": {}, "error": str(exc)[:400],
            }
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        record["cached"] = False
        if post and record.get("output"):
            record = dict(record, output=post(record["output"]))
        return record

    if spec["kind"] == "local":
        return [one(c) for c in clips]

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            records = list(pool.map(one, clips))
        by_id = {r["clip_id"]: r for r in records}
        return [by_id[c["clip_id"]] for c in clips]

    # Sequential, with a circuit breaker. A free endpoint whose upstream provider is
    # saturated fails every call however long the backoff runs, and waiting out the full
    # retry ladder 49 times turns one dead model into an hour of nothing. Give up after
    # BREAKER_LIMIT consecutive failures with no success at all, and record the rest as
    # unavailable so the report can say so instead of publishing a WER built from
    # fallbacks.
    # Trip on the failure *rate*, not on a run of consecutive failures. A saturated free
    # endpoint lets the occasional call through, and an earlier version of this breaker
    # required zero successes ever -- so gemma-4-31b:free, which answered 3 of 49 attempts,
    # reset the counter often enough to never trip and would have ground on for hours.
    BREAKER_MIN_FAILURES = 8
    BREAKER_MIN_RATE     = 0.25
    records: list[dict] = []
    failures = successes = 0
    tripped = False
    for clip in clips:
        if tripped:
            records.append({"clip_id": clip["clip_id"], "raw": "", "output": "",
                            "elapsed": 0.0, "usage": {}, "cached": False,
                            "error": "skipped: endpoint unavailable"})
            continue
        record = one(clip)
        records.append(record)
        if record.get("cached"):
            continue
        if record["error"]:
            failures += 1
        else:
            successes += 1
        attempted = failures + successes
        if failures >= BREAKER_MIN_FAILURES and successes / attempted < BREAKER_MIN_RATE:
            tripped = True
            print(f"[unavailable: {successes}/{attempted}] ", end="", flush=True)
    return records


def score(clips: list[dict], records: list[dict]) -> dict:
    """Pooled WER plus the guard metrics: what regressed, and what grew."""
    total_edits = total_words = 0
    per_clip: list[dict] = []
    errors = 0

    for clip, record in zip(clips, records):
        if record["error"]:
            errors += 1
            # Scored as the uncorrected hypothesis: a failed call means no correction was
            # applied, which is what production would see. Dropping the clip instead would
            # quietly shrink the corpus and make an unreliable model look better.
            output = clip["hypothesis"]
        else:
            output = record["output"] or clip["hypothesis"]

        malformed = bool(record["error"]) or is_malformed(
            record.get("raw", ""), output, clip["hypothesis"])

        before = bench.word_error_counts(clip["reference"], clip["hypothesis"])
        after = bench.word_error_counts(clip["reference"], output)
        if before is None or after is None:
            continue
        total_edits += after[0]
        total_words += after[1]
        per_clip.append({
            "clip_id": clip["clip_id"],
            "before": before[0] / before[1],
            "after": after[0] / after[1],
            "delta": (after[0] - before[0]) / before[1],
            "edits_after": after[0],
            "ref_words": after[1],
            "hyp_words": len(bench._normalize(clip["hypothesis"])),
            "out_words": len(bench._normalize(output)),
            "malformed": malformed,
            "output": output,
        })

    improved = sum(1 for p in per_clip if p["after"] < p["before"] - 1e-9)
    regressed = sum(1 for p in per_clip if p["after"] > p["before"] + 1e-9)
    grew = sum(1 for p in per_clip if p["out_words"] > p["hyp_words"] * 1.25 + 2)
    shrank = sum(1 for p in per_clip if p["out_words"] < p["hyp_words"] * 0.75 - 2)

    latencies = sorted(r["elapsed"] for r in records if not r["error"] and not r.get("cached"))
    return {
        "pooled_wer": total_edits / total_words if total_words else None,
        "edits": total_edits,
        "words": total_words,
        "improved": improved,
        "regressed": regressed,
        "grew": grew,
        "shrank": shrank,
        "errors": errors,
        "malformed": sum(1 for p in per_clip if p["malformed"]),
        "median_latency": latencies[len(latencies) // 2] if latencies else None,
        "per_clip": per_clip,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", default=str(HERE / "bench-results-groq.json"),
                        help="bench.py results JSON holding the raw hypotheses")
    parser.add_argument("--config", default="groq_prompt", help="config key within that JSON")
    parser.add_argument("--contenders", default="all",
                        help="comma-separated names, or 'all' (%s)" % ",".join(CONTENDERS))
    parser.add_argument("--limit", type=int, default=0, help="only the first N clips (smoke test)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", default=str(HERE / "bench-correct-results.json"))
    args = parser.parse_args()

    global DATASET
    DATASET = Path(args.results).stem

    names = list(CONTENDERS) if args.contenders == "all" else \
        [n.strip() for n in args.contenders.split(",") if n.strip()]
    unknown = [n for n in names if n not in CONTENDERS]
    if unknown:
        print(f"unknown contender(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    clips = load_clips(Path(args.results), args.config)
    if args.limit:
        clips = clips[:args.limit]
    print(f"{len(clips)} clips with references from {Path(args.results).name} [{args.config}]\n")

    baseline = None
    summaries: dict[str, dict] = {}
    for name in names:
        spec = CONTENDERS[name]
        print(f"  {name:<14} {spec['label']:<42} ", end="", flush=True)
        started = time.monotonic()
        records = run_contender(name, spec, clips, args.workers)
        summary = score(clips, records)
        summary["label"] = spec["label"]
        summaries[name] = summary
        if baseline is None:
            baseline = {"pooled_wer": None}
        print(f"WER {summary['pooled_wer']:.3f}  "
              f"+{summary['improved']}/-{summary['regressed']}  "
              f"err {summary['errors']}  ({time.monotonic() - started:.0f}s)")

    # The uncorrected hypotheses, scored the same way, so every row has something to beat.
    raw_edits = raw_words = 0
    for clip in clips:
        counts = bench.word_error_counts(clip["reference"], clip["hypothesis"])
        if counts:
            raw_edits += counts[0]
            raw_words += counts[1]
    raw_wer = raw_edits / raw_words if raw_words else None

    print(f"\n{'contender':<16}{'pooled WER':>12}{'vs raw':>10}{'better':>8}{'worse':>7}"
          f"{'grew':>6}{'err':>5}{'p50 s':>8}")
    print("-" * 72)
    print(f"{'(uncorrected)':<16}{raw_wer:>11.1%}{'':>10}{'':>8}{'':>7}{'':>6}{'':>5}{'':>8}")
    for name in names:
        s = summaries[name]
        # A model that answered for only some clips has no honest pooled WER: the clips it
        # dropped are scored uncorrected, so the number would describe the corpus, not the
        # model. Say "unavailable" rather than publishing a figure that looks comparable.
        if s["errors"] > len(clips) // 2:
            print(f"{name:<16}{'unavailable':>11}{'':>10}{'':>8}{'':>7}{'':>6}{s['errors']:>5}{'':>8}")
            continue
        delta = (s["pooled_wer"] - raw_wer) * 100
        lat = f"{s['median_latency']:.2f}" if s["median_latency"] else "-"
        print(f"{name:<16}{s['pooled_wer']:>11.1%}{delta:>+9.1f}p{s['improved']:>8}"
              f"{s['regressed']:>7}{s['grew']:>6}{s['errors']:>5}{lat:>8}")

    # Like-for-like table.
    #
    # A model that answers 45 clips and emits chain-of-thought on 3 cannot be compared to
    # one that answered all 49: the malformed replies dominate a pooled figure completely.
    # Restricting every contender to the clips where all of them produced a well-formed
    # reply gives one number they can honestly be ranked on. The malformed count stays
    # visible beside it, because reliability is a property of the model too, not a footnote.
    clean_ids = {c["clip_id"] for c in clips}
    for name in names:
        clean_ids &= {p["clip_id"] for p in summaries[name]["per_clip"] if not p["malformed"]}

    if len(clean_ids) < len(clips):
        raw_e = raw_w = 0
        for clip in clips:
            if clip["clip_id"] not in clean_ids:
                continue
            counts = bench.word_error_counts(clip["reference"], clip["hypothesis"])
            if counts:
                raw_e += counts[0]
                raw_w += counts[1]
        clean_raw = raw_e / raw_w if raw_w else None

        print(f"\nWell-formed subset: {len(clean_ids)} of {len(clips)} clips "
              f"(every contender answered these with a transcript)")
        print(f"{'contender':<16}{'pooled WER':>12}{'vs raw':>10}{'better':>8}{'worse':>7}"
              f"{'malformed':>11}")
        print("-" * 64)
        print(f"{'(uncorrected)':<16}{clean_raw:>11.1%}")
        for name in names:
            rows = [p for p in summaries[name]["per_clip"] if p["clip_id"] in clean_ids]
            edits = sum(p["edits_after"] for p in rows)
            words = sum(p["ref_words"] for p in rows)
            wer = edits / words if words else None
            better = sum(1 for p in rows if p["after"] < p["before"] - 1e-9)
            worse = sum(1 for p in rows if p["after"] > p["before"] + 1e-9)
            summaries[name]["clean_pooled_wer"] = wer
            print(f"{name:<16}{wer:>11.1%}{(wer - clean_raw) * 100:>+9.1f}p"
                  f"{better:>8}{worse:>7}{summaries[name]['malformed']:>11}")

    Path(args.out).write_text(json.dumps({
        "results_file": Path(args.results).name,
        "config": args.config,
        "clips": len(clips),
        "raw_pooled_wer": raw_wer,
        "system_prompt": SYSTEM_PROMPT,
        "contenders": summaries,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
