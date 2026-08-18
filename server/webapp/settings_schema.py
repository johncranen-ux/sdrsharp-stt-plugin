"""What the control panel is allowed to expose, and how each value is validated.

Scope is the settings start-all.bat names -- 27 of the 65 environment variables the proxy
reads. That file is the curated operator surface: a setting becomes operator-facing by being
added there with the prose comment that explains it, so this catalogue inherits that
documentation rather than competing with it.

Every value is stored as a STRING, because that is what an environment variable is. The type
exists to validate input and to render a control, never to change the storage format.
"""
from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class SettingType(str, enum.Enum):
    SECRET = "secret"
    TEXT = "text"
    INT = "int"
    BOOL = "bool"
    ENUM = "enum"
    BBOX = "bbox"
    PATH = "path"


class SettingSpec(BaseModel):
    key: str
    type: SettingType
    default: str
    group: str
    description: str
    choices: list[str] | None = None
    minimum: int | None = None
    maximum: int | None = None


BOOL_CHOICES = ("on", "off")


def validate_value(spec: SettingSpec, raw: str) -> str:
    """Return the normalised value, or raise ValueError naming the setting."""
    value = (raw or "").strip()

    if spec.type is SettingType.BOOL:
        if value.lower() not in BOOL_CHOICES:
            raise ValueError(f"{spec.key}: expected 'on' or 'off', got {raw!r}")
        return value.lower()

    if spec.type is SettingType.ENUM:
        if value not in (spec.choices or []):
            raise ValueError(
                f"{spec.key}: expected one of {', '.join(spec.choices or [])}, got {raw!r}")
        return value

    if spec.type is SettingType.INT:
        try:
            number = int(value)
        except ValueError:
            raise ValueError(f"{spec.key}: expected a whole number, got {raw!r}") from None
        if spec.minimum is not None and number < spec.minimum:
            raise ValueError(f"{spec.key}: must be at least {spec.minimum}, got {number}")
        if spec.maximum is not None and number > spec.maximum:
            raise ValueError(f"{spec.key}: must be at most {spec.maximum}, got {number}")
        return str(number)

    if spec.type is SettingType.BBOX:
        parts = [p.strip() for p in value.split(",")]
        if len(parts) != 4:
            raise ValueError(
                f"{spec.key}: expected latmin,latmax,lonmin,lonmax, got {raw!r}")
        try:
            latmin, latmax, lonmin, lonmax = (float(p) for p in parts)
        except ValueError:
            raise ValueError(f"{spec.key}: all four bounds must be numbers, got {raw!r}") from None
        if not (-90 <= latmin <= 90 and -90 <= latmax <= 90):
            raise ValueError(f"{spec.key}: latitude out of range in {raw!r}")
        if not (-180 <= lonmin <= 180 and -180 <= lonmax <= 180):
            raise ValueError(f"{spec.key}: longitude out of range in {raw!r}")
        # Inverted bounds return an empty vessel box, which looks exactly like a dead feed.
        if latmin >= latmax:
            raise ValueError(f"{spec.key}: latmin must be below latmax, got {raw!r}")
        if lonmin >= lonmax:
            raise ValueError(f"{spec.key}: lonmin must be below lonmax, got {raw!r}")
        return ",".join(parts)

    return value


SETTINGS: list[SettingSpec] = [
    # ---- Secrets -------------------------------------------------------------
    SettingSpec(key="ANTHROPIC_API_KEY", type=SettingType.SECRET, default="", group="Secrets",
                description="Enables conversation resolution and correction. Unset disables "
                            "identification entirely."),
    SettingSpec(key="GROQ_API_KEY", type=SettingType.SECRET, default="", group="Secrets",
                description="Required when STT_BACKEND is groq, which is the default."),
    SettingSpec(key="OPENROUTER_API_KEY", type=SettingType.SECRET, default="", group="Secrets",
                description="Alternative LLM provider for the correction pass."),
    SettingSpec(key="AISSTREAM_API_KEY", type=SettingType.SECRET, default="", group="Secrets",
                description="Only used when AIS_SOURCE is aisstream."),
    SettingSpec(key="AISSTREAM_API_KEY2", type=SettingType.SECRET, default="", group="Secrets",
                description="Second aisstream key, used as a fallback."),
    SettingSpec(key="AISHUB_USERNAME", type=SettingType.SECRET, default="", group="Secrets",
                description="AISHub username, issued for a station contributing an AIS feed. "
                            "Signing up alone is not enough."),

    # ---- STT -----------------------------------------------------------------
    SettingSpec(key="STT_BACKEND", type=SettingType.ENUM, default="groq", group="STT",
                choices=["groq", "whisper_cpp"],
                description="groq is Groq's hosted Whisper API, no GPU involved, and is what "
                            "this deployment uses. whisper_cpp is a local whisper.cpp server "
                            "on an AMD GPU under WSL2 -- fully supported for anyone running "
                            "this with their own hardware. Changing this needs a restart."),
    SettingSpec(key="GROQ_MODEL", type=SettingType.TEXT, default="whisper-large-v3", group="STT",
                description="Groq's Whisper model. large-v3 measured 17.1% pooled WER on "
                            "235 English clips."),
    SettingSpec(key="WHISPER_BACKEND_PORT", type=SettingType.INT, default="8080", group="STT",
                minimum=1, maximum=65535,
                description="Port the local whisper.cpp server listens on inside WSL. Only "
                            "used when STT_BACKEND is whisper_cpp."),

    # ---- AIS source ----------------------------------------------------------
    SettingSpec(key="AIS_SOURCE", type=SettingType.ENUM, default="aishub", group="AIS source",
                choices=["aishub", "aisstream"],
                description="Where vessel data comes from. aishub polls a REST API; aisstream "
                            "is a websocket feed, kept live and tested so reverting works."),
    SettingSpec(key="AISHUB_BBOX", type=SettingType.BBOX, default="51.4,52.6,2.0,4.25",
                group="AIS source",
                description="latmin,latmax,lonmin,lonmax. The sea box, set 2026-08-13: Maas "
                            "Approach works ships at sea, never river traffic already inside. "
                            "The old wide box (51.0,53.2,2.0,6.0) carried the whole Rhine/Maas "
                            "inland network -- 8,381 vessels with 685 duplicate-name groups "
                            "against this box's 1,537 and 43, a 94% cut in the name collisions "
                            "that cause misidentification. The east edge is 4.25, PAST Hoek van "
                            "Holland (4.12), on purpose: MINERAL JINDEOK was at 4.113 while "
                            "calling."),
    SettingSpec(key="AISHUB_POLL_SEC", type=SettingType.INT, default="900", group="AIS source",
                minimum=60, maximum=86400,
                description="Seconds between AISHub polls. Values under 60 are refused: AISHub "
                            "answers a faster caller with no data at all."),
    SettingSpec(key="AIS_SILENCE_WARN_SEC", type=SettingType.INT, default="0", group="AIS source",
                minimum=0, maximum=86400,
                description="Warn when a CONNECTED AIS feed stops delivering -- the failure "
                            "that otherwise looks identical to a quiet channel. 0 is off. "
                            "Applies to the aisstream path only; AISHub reports its own failed "
                            "polls. Six days were once lost to a feed that failed quietly."),
    SettingSpec(key="AIS_CACHE_FILE", type=SettingType.PATH, default="", group="AIS source",
                description="Override where the vessel cache lives. Leave EMPTY for production. "
                            "A bench must point at a frozen snapshot from the week its labels "
                            "cover; arms measured against different caches are not comparable."),

    # ---- Identification ------------------------------------------------------
    SettingSpec(key="CONVERSATION_RESOLVER", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Decide vessel identity after each exchange ends, from the whole "
                            "exchange rather than one transmission. Never touches the live "
                            "transcript."),
    SettingSpec(key="AIS_HINT_FILTER", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Stops ordinary speech ('good day') being matched to real ships."),
    SettingSpec(key="AIS_NAME_FILTER", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Stops a mis-heard name matching a short vessel spelled inside it "
                            "('Orason' -> RA). Off restores the old WRatio scorer at cutoff 80."),
    SettingSpec(key="AIS_PARTIAL_CALLSIGN", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Identify a vessel from a partly-garbled spelled-out callsign when "
                            "a spoken name agrees. Off restores exact-callsign matching only."),
    SettingSpec(key="RESOLVER_LIVE_CANDIDATES", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Offer the resolver the vessel the live per-transmission pass "
                            "already matched, as a lead rather than a verdict."),
    SettingSpec(key="PROMPT_ECHO_FILTER", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Drops transcriptions that are the decoding prompt read back."),
    SettingSpec(key="AIS_LIVE_MATCH_MAX_AGE_MIN", type=SettingType.INT, default="360",
                group="Identification", minimum=0, maximum=100000,
                description="How old a vessel's AIS fix may be for the live pass to re-offer "
                            "it. Age counts from the last SUCCESSFUL poll, not the wall clock, "
                            "so a stalled feed freezes the cutoff instead of ageing every ship "
                            "out at once. Measured 2026-08-18: precision 87.1 -> 88.3, six "
                            "false positives removed, nothing lost. 0 disables the bound."),
    SettingSpec(key="AIS_CALLSIGN_SUFFIX_FALLBACK", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Try the TAIL of a spelled-out callsign that decoded cleanly but "
                            "short ('call SUNvictor seven' swallowed the V of V7B2710). The "
                            "tail must fit exactly one cached callsign AND a resembling name "
                            "must be spoken in the same conversation. On by decision rather "
                            "than by measurement -- if identification regresses, switch this "
                            "off first."),
    SettingSpec(key="AIS_SUGGEST", type=SettingType.BOOL, default="on", group="Identification",
                description="Under a conversation nobody was identified in, show the best few "
                            "vessel names found BELOW the identification cutoff, labelled "
                            "unconfirmed. Never names anyone. Right ship in the list 9 times "
                            "out of 35."),
    SettingSpec(key="AIS_SUGGEST_N", type=SettingType.INT, default="3", group="Identification",
                minimum=1, maximum=10,
                description="How many possible matches to list. 5 finds 3 more of the 35 at "
                            "the cost of two more wrong names to read each time."),
    SettingSpec(key="AIS_SUGGEST_TIEBREAK", type=SettingType.BOOL, default="off",
                group="Identification",
                description="Rank equally-scoring suggestions by plausibility instead of "
                            "arbitrarily. OFF: it could not be measured, because scoring "
                            "proximity needs each vessel's position AT THE TIME and a frozen "
                            "cache keeps only the latest fix."),

    # ---- Ports ---------------------------------------------------------------
    SettingSpec(key="PROXY_PORT", type=SettingType.INT, default="9000", group="Ports",
                minimum=1, maximum=65535,
                description="Port the proxy listens on. The SDR# plugin must be pointed at the "
                            "same port."),
]

BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SETTINGS}
