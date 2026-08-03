"""Identifying the vessel in a single transmission, live.

Deliberately sees only the transmission in front of it. Conversation context in this call
bleeds into the transcription it also produces -- that was measured, and it is why
cross-turn identity is settled afterwards by stt_proxy.conversations instead.

Two rules here exist because the pipeline was previously inventing vessels rather than
Claude doing so: AIS hints are candidates for correcting a name that was actually said,
never a source of names; and a callsign is only accepted if it can be read back out of the
transmission, since an invented one looks up to a real ship.
"""

import datetime
import json
import re

from stt_proxy.ais import _find_ais_hints, _get_ship_type_name, match_by_callsign, match_by_name
from stt_proxy.claude import _get_claude
from stt_proxy.corrections import _apply_sttt_corrections, _callsign_supported_by_text


#
# The extractor used to be told to "always extract callsigns", and it obliged even when the
# transmission contained none: "Gungor Star one three one five, correct." produced VRSQ4,


SYSTEM_PROMPT = """\
You analyse VHF marine radio transcriptions from Rotterdam harbour (Maas Approach / Rotterdam VTS area).
Correct fuzzy STT errors using maritime context. Return ONLY raw JSON, no markdown:
{"vessel": "<name or null>", "callsign": "<callsign or null>", "vessel_type": "<type or null>", "text": "<corrected text>"}

Rules:
1. Shore stations (Maas Approach, Rotterdam VTS, Pilot) are NOT vessels.
2. Extract vessel names: after "this is", "calling", vessel type words, or when shore station
   addresses a vessel. A type word is a marker for where the name starts, not part of it:
   "this is Motor Vessel GH Nightingale" gives the vessel "GH Nightingale", not "Motor Vessel
   GH Nightingale". The type belongs in "vessel_type".
3. Extract a callsign ONLY when the transmission spells one out -- phonetically
   ("Juliet Lima Sierra Romeo"), as characters ("9 Hotel Alpha six one"), or verbatim
   ("9HF5093"). If no callsign was spoken, return null. Do not guess one from the vessel
   name, from the AIS hints, or from anything else: a callsign nobody said is worse than
   no callsign, because it looks up to a real ship.
4. vessel_type: tanker/bulker/container/tug/ferry/general_cargo/passenger/yacht/pilot/null.
5. [AIS: ...] hints are nearby vessels, NOT a list of who is speaking. Only use a hint to
   fix the spelling of a name the speaker actually said. Never take a vessel name from the
   hints alone: if the transmission does not name a vessel, return null even when hints
   are present. "Yes, good day sir" names no vessel, whatever the hints say.
6. "text" is a transcription of THIS transmission and nothing else. Fix mis-heard words,
   but never add content: no vessel name that was not spoken here, no completing of a
   half-finished sentence. If the whole transmission was "Maas Approach." then "text" is
   "Maas Approach." -- NOT "Maas Approach, <vessel>." Identifying the speaker is what the
   "vessel" field is for.

Correcting "text":
The audio is noisy FM and the speakers are mostly non-native English speakers (Dutch,
Greek, Filipino, Russian, Indian, Turkish). Correct only what is clearly mis-recognised:
- Rotterdam names: Maas Approach, Maas Center, Rotterdam VTS, Pilot Rotterdam, Botlek,
  Europoort, Maasvlakte, Steenbank, Hoek van Holland, Caland, Beneluxhaven, Scheveningen,
  Recon buoy, Echo 1 / Echo 3 buoys, Deepwater Route.
- Procedure words: over, out, roger, wilco, standing by, stand by, copy, understood,
  say again, this is.
- NATO phonetic alphabet: Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliet
  Kilo Lima Mike November Oscar Papa Quebec Romeo Sierra Tango Uniform Victor Whiskey
  X-ray Yankee Zulu.
- Maritime vocabulary: draught (not draft), buoy (not boy), anchor, heave up, pilot ladder,
  starboard, port side, inbound, outbound, southbound, motor vessel, Motortanker, callsign,
  ETA, pilot boarding ground.

Then:
a. Make the smallest edit that fixes a clear error. If a word is merely unusual, or you are
   unsure what was meant, leave it exactly as it is.
b. Never remove content. Every utterance in the transmission must survive into "text", even
   if it is garbled, redundant or a filler. "Okay, understood. One five zero zero, Pilot."
   keeps its "Okay, understood." -- dropping a phrase is as wrong as inventing one.
c. Keep the speaker's own words, word order, grammar and disfluencies. You are not
   rewriting or improving the English.
d. Numbers stay in exactly the form they were transcribed, in both directions: do not turn
   "one eight zero zero" into "1800", and do not turn "4.7" into "four point seven".
e. The channels here are "zero one" (channel 01, the Maas Approach working channel) and
   "one six" (channel 16, the calling channel). Vessels are routinely told to stand by on
   both and say so as "zero one, one six" or "one and one six". That is correct as
   transcribed: never rewrite it to "channel one six", never drop the "zero one", and never
   insert the word "channel" where it was not spoken. Only read digits as a channel when the
   transmission is about standing by or switching -- a time, a draught or an ETA containing
   similar digits ("one five zero zero, pilot") is not a channel and must be left alone.
f. A transmission may be a single word, a fragment, or badly garbled. Correct it as given
   and return it anyway; never ask for input and never comment on the transmission.
"""


# The schema asks for "<name or null>", and the model sometimes answers with the *string*
# "null" rather than a JSON null. A string is truthy, so it survives every `or` fallback
# downstream and reaches the display as a real value -- "[GH NIGHTINGALE/null]" in the plugin,
# and a vessel literally named "None" would be looked up against AIS. Coerced here, at the one
# point every field passes through, rather than guarded at each use.
_PLACEHOLDER_VALUES = {"null", "none", "n/a", "unknown", "-", ""}


def _null_out_placeholders(result: dict) -> None:
    for field in ("vessel", "callsign", "vessel_type"):
        value = result.get(field)
        if isinstance(value, str) and value.strip().lower() in _PLACEHOLDER_VALUES:
            result[field] = None


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
            # Without this the call runs at the API default of 1.0, and this call rewrites
            # the transcript the plugin displays. Measured: two runs of a byte-identical
            # prompt over the same 49 clips scored 38.8% and 39.7% pooled WER -- nearly a
            # point of variation from sampling alone, enough to swamp a real difference
            # between two candidate prompts.
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        content = message.content[0].text.strip()
        if "```" in content:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
            if m:
                content = m.group(1)
        result = json.loads(content)
        _null_out_placeholders(result)
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
