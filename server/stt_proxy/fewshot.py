"""Few-shot examples for the conversation-correction prompt.

Loaded at runtime, never baked into source. The examples that actually teach this task are
real exchanges, and real exchanges are received radio traffic: the repo's CI gate is a list of
known filenames rather than a content scan, so an example pasted into a module would pass the
gate and still put traffic into git permanently (NL Telecommunicatiewet 18.13 / ITU RR 17.3).

The synthetic set below is invented, deliberately names nobody real, and exists so a fresh
checkout works without the operator's private files.

IMPORTANT: The file named by CONVERSATION_FEWSHOT_FILE must sit at a path matching the patterns
ignored in .gitignore (server/*fewshot*.json, server/*examples*.json). The CI transcript gate is
a hard-coded filename list, not a content scanner; an unmatched path bypasses the gate and can
commit received radio traffic to a public repo.
"""

import json
import os

# Invented vessels. A real cached name here would invite the model to reach for it in
# unrelated conversations -- the same failure the live prompt's rule 5 guards against for
# AIS hints.
SYNTHETIC_EXAMPLES = [
    {
        "vessel": "EXAMPLE TRADER",
        "turns": [
            {"id": 1, "text": "Maas Approach, Maas Approach, motor vision Example Traitor."},
            {"id": 2, "text": "Motorvessel Example Trader, Maas Approach, good morning."},
        ],
        "output": {"turns": [
            {"id": 1, "text": "Maas Approach, Maas Approach, Motorvessel Example Trader.",
             "changes": [
                 {"from": "motor vision", "to": "Motorvessel",
                  "reason": "shore station rendition of the type word"},
                 {"from": "Example Traitor", "to": "Example Trader",
                  "reason": "shore station rendition of the name"}]},
            {"id": 2, "text": "Motorvessel Example Trader, Maas Approach, good morning.",
             "changes": []},
        ]},
    },
    {
        "vessel": "EXAMPLE VOYAGER",
        "turns": [
            {"id": 1, "text": "Example Voyager, pilot ladder port side one metre above water."},
            {"id": 2, "text": "Pilot letter part side one metre above water, Example Voyager."},
        ],
        "output": {"turns": [
            {"id": 1, "text": "Example Voyager, pilot ladder port side one metre above water.",
             "changes": []},
            {"id": 2, "text": "Pilot ladder port side one metre above water, Example Voyager.",
             "changes": [
                 {"from": "Pilot letter part side", "to": "Pilot ladder port side",
                  "reason": "garbled readback of the instruction in turn 1"}]},
        ]},
    },
]


def load_examples(path: str | None = None) -> list[dict]:
    """Examples from `path` (or CONVERSATION_FEWSHOT_FILE), else the synthetic set.

    Every failure falls back rather than raising: a missing or hand-edited examples file must
    never stop the pass from running.
    """
    path = path or os.environ.get("CONVERSATION_FEWSHOT_FILE", "")
    if not path:
        return SYNTHETIC_EXAMPLES
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return SYNTHETIC_EXAMPLES
    if not isinstance(loaded, list) or not loaded:
        return SYNTHETIC_EXAMPLES
    return loaded


def render_examples(examples: list[dict]) -> str:
    """The examples as prompt text. Empty string for no examples, so the caller can concatenate."""
    blocks = []
    for example in examples:
        lines = ["[EXAMPLE INPUT]", f"vessel: {example.get('vessel') or 'unidentified'}"]
        for turn in example.get("turns", []):
            lines.append(f"  {turn['id']}. {turn['text']}")
        lines.append("[EXAMPLE OUTPUT]")
        lines.append(json.dumps(example.get("output", {}), ensure_ascii=False))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
