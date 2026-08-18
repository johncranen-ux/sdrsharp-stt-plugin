"""What the control panel is allowed to expose, and how each value is validated.

Scope is the settings start-all.bat names -- 26 of the 65 environment variables the proxy
reads. That file is the curated operator surface: a setting becomes operator-facing by being
added there with the prose comment that explains it, so this catalogue inherits that
documentation rather than competing with it.

Every value is stored as a STRING, because that is what an environment variable is. The type
exists to validate input and to render a control, never to change the storage format.
"""
from __future__ import annotations

import enum

from pydantic import BaseModel


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
    exported: bool = True
    """False for settings the web app consumes itself. A child process is configured by
    environment variables; a setting the proxy never reads must not appear in its
    environment, and the app's own bind address must never be visible to a child at all."""


BOOL_CHOICES = ("on", "off")


def _shown(spec: SettingSpec, raw: str) -> str:
    """The value as it may safely appear in a ValueError message.

    No SECRET path raises today, but Phase 2 renders these into an HTTP response and a log,
    and one added check on a secret would otherwise leak it.
    """
    return "<redacted>" if spec.type is SettingType.SECRET else repr(raw)


def validate_value(spec: SettingSpec, raw: str) -> str:
    """Return the normalised value, or raise ValueError naming the setting."""
    value = (raw or "").strip()

    if spec.type is SettingType.BOOL:
        if value.lower() not in BOOL_CHOICES:
            raise ValueError(f"{spec.key}: expected 'on' or 'off', got {_shown(spec, raw)}")
        return value.lower()

    if spec.type is SettingType.ENUM:
        if value not in (spec.choices or []):
            raise ValueError(
                f"{spec.key}: expected one of {', '.join(spec.choices or [])}, "
                f"got {_shown(spec, raw)}")
        return value

    if spec.type is SettingType.INT:
        try:
            number = int(value)
        except ValueError:
            raise ValueError(
                f"{spec.key}: expected a whole number, got {_shown(spec, raw)}") from None
        if spec.minimum is not None and number < spec.minimum:
            raise ValueError(f"{spec.key}: must be at least {spec.minimum}, got {number}")
        if spec.maximum is not None and number > spec.maximum:
            raise ValueError(f"{spec.key}: must be at most {spec.maximum}, got {number}")
        return str(number)

    if spec.type is SettingType.BBOX:
        parts = [p.strip() for p in value.split(",")]
        if len(parts) != 4:
            raise ValueError(
                f"{spec.key}: expected latmin,latmax,lonmin,lonmax, got {_shown(spec, raw)}")
        try:
            latmin, latmax, lonmin, lonmax = (float(p) for p in parts)
        except ValueError:
            raise ValueError(
                f"{spec.key}: all four bounds must be numbers, got {_shown(spec, raw)}") from None
        if not (-90 <= latmin <= 90 and -90 <= latmax <= 90):
            raise ValueError(f"{spec.key}: latitude out of range in {_shown(spec, raw)}")
        if not (-180 <= lonmin <= 180 and -180 <= lonmax <= 180):
            raise ValueError(f"{spec.key}: longitude out of range in {_shown(spec, raw)}")
        # Inverted bounds return an empty vessel box, which looks exactly like a dead feed.
        if latmin >= latmax:
            raise ValueError(f"{spec.key}: latmin must be below latmax, got {_shown(spec, raw)}")
        if lonmin >= lonmax:
            raise ValueError(f"{spec.key}: lonmin must be below lonmax, got {_shown(spec, raw)}")
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
                description="Second aisstream key kept in the launcher. NOTE: no code currently reads this -- only AISSTREAM_API_KEY is used. Retained because it is set in start-all.bat."),
    SettingSpec(key="AISHUB_USERNAME", type=SettingType.SECRET, default="", group="Secrets",
                description="AISHub username, issued for a station contributing an AIS feed. "
                            "Signing up alone is not enough."),

    # ---- STT -----------------------------------------------------------------
    SettingSpec(key="STT_BACKEND", type=SettingType.ENUM, default="groq", group="STT",
                choices=["groq", "whisper_cpp"],
                description="groq is Groq's hosted Whisper API, no GPU involved, and is what "
                            "this deployment uses. whisper_cpp is a local whisper.cpp server "
                            "on an AMD GPU under WSL2 -- fully supported for anyone running "
                            "this with their own hardware."),
    SettingSpec(key="GROQ_MODEL", type=SettingType.TEXT, default="whisper-large-v3", group="STT",
                description="Groq's Whisper model. large-v3 measured 17.1% pooled WER on "
                            "235 English clips."),
    SettingSpec(key="WHISPER_BACKEND_PORT", type=SettingType.INT, default="8080", group="STT",
                minimum=1, maximum=65535,
                description="Port the local whisper.cpp server listens on inside WSL. Only "
                            "used when STT_BACKEND is whisper_cpp."),

    # ---- AIS source ----------------------------------------------------------
    SettingSpec(key="AIS_SOURCE", type=SettingType.ENUM, default="aishub", group="AIS source",
                choices=["aishub", "aisstream", "off"],
                description="Where vessel data comes from. aishub polls a REST API; aisstream "
                            "is a websocket feed, kept live and tested so reverting works. "
                            "off disables vessel matching entirely: transcription continues, but no conversation is given a vessel."),
    SettingSpec(key="AISHUB_BBOX", type=SettingType.BBOX, default="51.4,52.6,2.0,4.25",
                group="AIS source",
                description="latmin,latmax,lonmin,lonmax. The sea box, set 2026-08-13: Maas "
                            "Approach works ships at sea entering or waiting to enter, never river traffic already inside. "
                            "The old wide box (51.0,53.2,2.0,6.0) carried the whole Rhine/Maas "
                            "inland network -- 8,381 vessels with 685 duplicate-name groups "
                            "against this box's 1,537 and 43, a 94% cut in the name collisions "
                            "that cause misidentification, and the share of ships over 150 m rises from 5% to 14%. The east edge is 4.25, PAST Hoek van "
                            "Holland (4.12), on purpose: a boundary at the entrance would have excluded MINERAL JINDEOK, which was at 4.113 while "
                            "calling and whose spelled-out callsign proves it was on the channel."),
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

    # ---- Paths ---------------------------------------------------------------
    SettingSpec(key="CONVERSATIONS_FILE", type=SettingType.PATH, default="", group="Paths",
                description="Where resolved conversations are stored. Empty means "
                            "server/stt_proxy/conversations.json, next to the code. Set it to "
                            "move the data off the install directory before a host migration."),
    SettingSpec(key="VESSELS_LOG_FILE", type=SettingType.PATH, default="", group="Paths",
                description="The identified-vessels HTML log the proxy writes and serves at "
                            "/. Empty means server/identified_vessels.html."),
    SettingSpec(key="LOG_DIR", type=SettingType.PATH, default="", group="Paths",
                exported=False,
                description="Where managed processes write their stdout, one file per process "
                            "per day. Empty means server/logs. On a headless box this is the "
                            "only record of what a process said -- today the proxy runs under "
                            "cmd /k and its output dies with the window."),
    SettingSpec(key="SDRSHARP_DIR", type=SettingType.PATH, default=r"D:\SDR\SDRSharp",
                group="Paths", exported=False,
                description="Where SDR# is installed. Monitored, never managed: SDR# needs an "
                            "interactive desktop and its play button must be pressed by hand. "
                            "The panel only checks that this path resolves."),
    SettingSpec(key="CAPTURES_DIR", type=SettingType.PATH,
                default=r"D:\SDR\SDRSharp\Plugins\SttPlugin\captures", group="Paths",
                exported=False,
                description="Where the plugin writes captured audio, in dated subdirectories. "
                            "Checked for existence only; the panel never reads it."),
    SettingSpec(key="WHISPER_BACKEND_HOST", type=SettingType.TEXT, default="localhost",
                group="Paths",
                description="Host of the local whisper.cpp server, used only when "
                            "STT_BACKEND=whisper_cpp. localhost reaches WSL2 from Windows."),

    # ---- The AIS station -----------------------------------------------------
    SettingSpec(key="AIS_STATION_HOST", type=SettingType.TEXT, default="192.168.2.1",
                group="AIS station", exported=False,
                description="The PC running AIS-catcher. Its own box, on a DHCP reservation."),
    SettingSpec(key="AIS_STATION_HTTP_PORT", type=SettingType.INT, default="8100",
                group="AIS station", exported=False, minimum=1, maximum=65535,
                description="AIS-catcher's web UI port (-N). The counter polls /ships.json "
                            "there for its range map."),
    SettingSpec(key="AIS_STATION_NMEA_PORT", type=SettingType.INT, default="10111",
                group="AIS station", exported=False, minimum=1, maximum=65535,
                description="AIS-catcher's NMEA TCP output port (-P). The counter connects to "
                            "it to count distinct MMSIs per hour."),

    # ---- Managed processes ---------------------------------------------------
    SettingSpec(key="PROXY_ENABLED", type=SettingType.BOOL, default="on", group="Processes",
                exported=False,
                description="Whether the proxy appears on the dashboard as a startable "
                            "process. Disabled is not the same as stopped: a stopped process "
                            "is one the operator turned off and may want back."),
    SettingSpec(key="COUNTER_ENABLED", type=SettingType.BOOL, default="on", group="Processes",
                exported=False,
                description="Whether the AIS station counter is startable. It exists to "
                            "measure the local receiver's coverage and is expected to become "
                            "unnecessary once that receiver has proven itself -- switch this "
                            "off then rather than deleting anything."),

    # ---- The web app itself --------------------------------------------------
    SettingSpec(key="WEBAPP_BIND_HOST", type=SettingType.TEXT, default="127.0.0.1",
                group="Web app", exported=False,
                description="The address the control panel listens on. 127.0.0.1 means this "
                            "machine only. Widening it to 0.0.0.0 exposes a panel that starts "
                            "processes and holds six API keys, so with no password set the "
                            "app refuses to start rather than opening that window."),
    SettingSpec(key="WEBAPP_PORT", type=SettingType.INT, default="8787", group="Web app",
                exported=False, minimum=1, maximum=65535,
                description="The control panel's own port. Deliberately not 9000, which the "
                            "proxy owns."),
]

BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SETTINGS}
