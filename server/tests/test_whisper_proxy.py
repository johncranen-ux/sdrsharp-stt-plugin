"""Tests for whisper-proxy.py: hallucination filtering, STT corrections, and the
multipart parse/rebuild that lets the proxy own the whisper.cpp decoder parameters.

Run with: py -m pytest server/tests -v
"""

import asyncio
import datetime
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR  = Path(__file__).resolve().parent.parent
_MODULE_PATH = _SERVER_DIR / "whisper-proxy.py"

# whisper-proxy.py imports the stt_proxy package, so server/ must be importable.
sys.path.insert(0, str(_SERVER_DIR))


def _load_proxy_module():
    # whisper-proxy.py has a hyphen in its name, so it can't be `import`ed normally.
    spec = importlib.util.spec_from_file_location("whisper_proxy", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["whisper_proxy"] = module
    spec.loader.exec_module(module)
    return module


proxy = _load_proxy_module()

# Submodules are imported directly where a test needs to patch module-level state: a flag
# is read inside the module that owns it, so patching the re-export on `proxy` would have
# no effect. Patch the owner.
from stt_proxy import ais, backends, conversations, corrections, identify, vessel_log  # noqa: E402


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
    # "ladder" is mis-heard 14 times across the 636 benchmarked transmissions and never once
    # transcribed correctly as "letter"/"leather" -- see the block below.
    ("pilot letter in the starboard side", "pilot ladder in the starboard side"),
    ("Ports are leather two meters above the waterline.", "ladder"),
    ("Portside Leather to me, that's above the water", "Portside ladder"),
    ("Pilot Letter, port side two meters", "Pilot ladder"),
])
def test_apply_sttt_corrections(raw, expected_substring):
    result = proxy._apply_sttt_corrections(raw)
    assert expected_substring in result


# "ladder" -> "letter"/"leather"
#
# Measured over every benchmarked transmission carrying a reference (636 rows, 293 clips):
# the decoder produces "ladder" 38 times, "letter" 14 and "leather" once, while the ground
# truth contains "ladder" 15 times and "letter" exactly once -- and that one is a typo in the
# reference itself (clip 0143, "pilot  letter port side", corrected with this change). So in
# this traffic the words are never anything but a mis-heard "ladder", which is what makes an
# unguarded substitution safe -- the same shape as the existing "boy" -> "buoy" rule.
#
# The CH01 Claude pass already lists "pilot ladder" in its vocabulary and still leaves these
# alone: "leather" is an ordinary English word, so its own instruction to make only the
# smallest clearly-correct edit holds it back. A deterministic rule after that pass costs
# nothing and does not depend on model behaviour.

def test_ladder_correction_is_not_applied_to_airband():
    """Aircraft have no pilot ladders, and "letter" is ordinary speech there."""
    assert "letter" in proxy._apply_sttt_corrections("say again the letter", mode="airband")


@pytest.mark.parametrize("raw", [
    "letter of protest",
    "a letter of credit for the agent",
])
def test_a_letter_of_something_is_left_alone(raw):
    """Precautionary rather than measured: no such phrase occurs in the corpus, but these are
    real maritime documents and the guard costs nothing on the 14 cases that do occur."""
    assert proxy._apply_sttt_corrections(raw) == raw


# ---------------------------------------------------------------------------
# Parsing the aisstream feed
#
# This had no tests, and three fields were being read under the wrong key: `IMO`, `SOG` and
# `COG`, where the feed sends `ImoNumber`, `Sog` and `Cog`. They parsed to None on every
# message ever received -- 0 of 8,434 cached vessels had any of the three, while every
# correctly-cased key was 83-94% populated -- so /identified-vessels rendered a dash for IMO,
# speed and course from the day it was written. Payloads below follow aisstream.io's
# documented message shape; the point of these tests is the exact capitalisation.
# ---------------------------------------------------------------------------

@pytest.fixture
def ais_caches(monkeypatch):
    vessels, callsigns = {}, {}
    monkeypatch.setattr(ais, "_vessel_cache", vessels)
    monkeypatch.setattr(ais, "_callsign_cache", callsigns)
    # record() indexes by MMSI, and these tests reuse MMSI 1 across many cases. Without
    # resetting the index too, a later test's `record()` call finds the previous test's
    # (now-orphaned) entry object still sitting under that MMSI and updates it in place,
    # leaving the fresh `vessels` dict above empty.
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_name_index", {})
    # Feed-health state is module-global and its log is rate-limited, so without this a
    # test's output would depend on which tests ran before it.
    monkeypatch.setattr(ais, "_unknown_frames_logged", 0)
    monkeypatch.setattr(ais, "_last_message_at", None)
    return vessels, callsigns


def test_ship_static_data_is_parsed(ais_caches):
    vessels, callsigns = ais_caches
    ais._process_ais({
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": 215760000},
        "Message": {"ShipStaticData": {
            "Name": "PECHORA STAR", "CallSign": "9HA2788", "ImoNumber": 9123456, "Type": 89,
            "Dimension": {"A": 100, "B": 29, "C": 10, "D": 11},
        }},
    })
    entry = vessels["PECHORA STAR"]
    assert entry["mmsi"] == "215760000"
    assert entry["callsign"] == "9HA2788"
    assert entry["imo"] == 9123456, "read as ImoNumber, not IMO"
    assert (entry["length"], entry["beam"]) == (129, 21)
    assert callsigns["9HA2788"] is entry, "one object, so a position report updates both"


def test_draught_and_destination_are_parsed(ais_caches):
    """The two fields the traffic actually asks about: "what is your maximum draught" and
    where you are bound opens most CH01 exchanges."""
    vessels, _ = ais_caches
    ais._process_ais({
        "MessageType": "ShipStaticData", "MetaData": {"MMSI": 1},
        "Message": {"ShipStaticData": {"Name": "ANOUK", "MaximumStaticDraught": 4.5,
                                       "Destination": "ROTTERDAM@@@@@@@@@@@"}},
    })
    assert vessels["ANOUK"]["draught"] == 4.5, "metres as a double, not tenths"
    assert vessels["ANOUK"]["destination"] == "ROTTERDAM"


@pytest.mark.parametrize("raw,expected", [
    ("ROTTERDAM@@@@@@@@@@@", "ROTTERDAM"),
    ("COASTGUARD@@@@@@@@H", "COASTGUARD"),   # the documented example: padding, then noise
    ("NL RTM", "NL RTM"),                    # no padding at all
    ("  EUROPOORT  ", "EUROPOORT"),
    ("@@@@@@@@@@", None),                    # nothing but padding
    ("", None),
])
def test_destination_padding_is_stripped(raw, expected, ais_caches):
    """'@' is the AIS null character, so everything from the first one is padding."""
    vessels, _ = ais_caches
    ais._process_ais({"MessageType": "ShipStaticData", "MetaData": {"MMSI": 1},
                      "Message": {"ShipStaticData": {"Name": "X", "Destination": raw}}})
    assert vessels["X"]["destination"] == expected


def test_position_report_is_parsed(ais_caches):
    vessels, _ = ais_caches
    ais._process_ais({
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": 215760000, "ShipName": "PECHORA STAR"},
        "Message": {"PositionReport": {
            "Latitude": 51.92, "Longitude": 3.5378, "Sog": 8.2, "Cog": 43.0,
            "TrueHeading": 45,
        }},
    })
    entry = vessels["PECHORA STAR"]
    assert (entry["latitude"], entry["longitude"]) == (51.92, 3.5378)
    assert entry["sog"] == 8.2, "read as Sog, not SOG"
    assert entry["cog"] == 43.0, "read as Cog, not COG"
    assert entry["heading"] == 45


def test_a_position_report_updates_the_vessel_the_static_message_created(ais_caches):
    """The two message types arrive independently, and the callsign lookup must see the
    position too -- both caches hold the same object."""
    vessels, callsigns = ais_caches
    ais._process_ais({
        "MessageType": "ShipStaticData", "MetaData": {"MMSI": 1},
        "Message": {"ShipStaticData": {"Name": "ANOUK", "CallSign": "PABC", "Type": 80,
                                       "Dimension": {"A": 50, "B": 10, "C": 5, "D": 6}}},
    })
    ais._process_ais({
        "MessageType": "PositionReport", "MetaData": {"MMSI": 1, "ShipName": "ANOUK"},
        "Message": {"PositionReport": {"Latitude": 52.0, "Longitude": 4.0, "Sog": 3.3,
                                       "Cog": 180.0, "TrueHeading": 181}},
    })
    assert callsigns["PABC"]["sog"] == 3.3
    assert vessels["ANOUK"]["length"] == 60


def _static(name, mmsi=1, callsign="PABC"):
    return {"MessageType": "ShipStaticData", "MetaData": {"MMSI": mmsi},
            "Message": {"ShipStaticData": {"Name": name, "CallSign": callsign, "Type": 80,
                                           "Dimension": {"A": 50, "B": 10, "C": 5, "D": 6}}}}


def _position(name, mmsi=1, lat=52.0, lon=4.0):
    return {"MessageType": "PositionReport", "MetaData": {"MMSI": mmsi, "ShipName": name},
            "Message": {"PositionReport": {"Latitude": lat, "Longitude": lon, "Sog": 3.3,
                                           "Cog": 180.0, "TrueHeading": 181}}}


def test_static_data_does_not_erase_a_known_position(ais_caches):
    """The reverse order of the test above, and the one that was broken: the static branch
    assigned a fresh dict carrying no position, so a vessel that reported its position and
    then broadcast static data lost it. Static messages repeat every ~6 minutes, so this
    fired continuously and left 25% of the vessels in the labelled conversations with no
    position at all -- which is what made distance data unusable."""
    vessels, callsigns = ais_caches
    ais._process_ais(_position("ANOUK"))
    ais._process_ais(_static("ANOUK"))
    assert vessels["ANOUK"]["latitude"] == 52.0
    assert vessels["ANOUK"]["longitude"] == 4.0
    assert vessels["ANOUK"]["callsign"] == "PABC"      # and the static fields still arrive
    assert callsigns["PABC"] is vessels["ANOUK"]       # both caches share one object


def test_static_data_still_creates_a_vessel_never_seen_before(ais_caches):
    vessels, _ = ais_caches
    ais._process_ais(_static("NEWCOMER"))
    assert vessels["NEWCOMER"]["mmsi"] == "1"
    assert vessels["NEWCOMER"].get("latitude") is None


@pytest.mark.parametrize("message,name", [
    (_position("ANOUK"), "ANOUK"),
    (_static("BERTHA"), "BERTHA"),
])
def test_both_message_types_stamp_last_seen(ais_caches, message, name):
    vessels, _ = ais_caches
    ais._process_ais(message)
    stamped = vessels[name]["last_seen"]
    # Parsing it is the assertion: the format has to stay readable and comparable.
    datetime.datetime.strptime(stamped, "%Y-%m-%d %H:%M:%S")


# Age filter
#
# Off by default, so these all set AIS_MAX_AGE_MIN explicitly. It excludes at match time
# rather than deleting, so a badly chosen threshold cannot destroy data and the cost of the
# threshold stays measurable.

def _aged(name, minutes_ago, mmsi="1"):
    stamp = datetime.datetime.now() - datetime.timedelta(minutes=minutes_ago)
    return {"name": name, "callsign": "", "mmsi": mmsi, "type": None, "imo": None,
            "length": None, "beam": None, "latitude": 52.0, "longitude": 4.0,
            "last_seen": stamp.strftime("%Y-%m-%d %H:%M:%S")}


def test_age_filter_is_off_by_default_so_stale_vessels_still_match(ais_caches):
    vessels, _ = ais_caches
    vessels["WILSON DURNESS"] = _aged("WILSON DURNESS", minutes_ago=10_000)
    assert ais.AIS_MAX_AGE_MIN == 0
    assert proxy.match_by_name("WILSON DURNESS")["name"] == "WILSON DURNESS"


def test_age_filter_excludes_a_vessel_not_heard_from_recently(ais_caches, monkeypatch):
    vessels, _ = ais_caches
    monkeypatch.setattr(ais, "AIS_MAX_AGE_MIN", 15)
    vessels["WILSON DURNESS"] = _aged("WILSON DURNESS", minutes_ago=60)
    assert proxy.match_by_name("WILSON DURNESS") is None
    assert proxy._find_ais_hints("this is WILSON DURNESS calling") == []


def test_age_filter_keeps_a_recently_heard_vessel(ais_caches, monkeypatch):
    vessels, _ = ais_caches
    monkeypatch.setattr(ais, "AIS_MAX_AGE_MIN", 15)
    vessels["WILSON DURNESS"] = _aged("WILSON DURNESS", minutes_ago=2)
    assert proxy.match_by_name("WILSON DURNESS")["name"] == "WILSON DURNESS"
    assert [h["name"] for h in proxy._find_ais_hints("this is WILSON DURNESS calling")]         == ["WILSON DURNESS"]


def test_unknown_age_is_not_treated_as_recent(ais_caches, monkeypatch):
    """Every entry written before last_seen existed lacks it. Counting those as fresh would
    make the filter silently do nothing against exactly the cache it would first meet."""
    vessels, _ = ais_caches
    monkeypatch.setattr(ais, "AIS_MAX_AGE_MIN", 15)
    entry = _aged("WILSON DURNESS", minutes_ago=0)
    del entry["last_seen"]
    vessels["WILSON DURNESS"] = entry
    assert proxy.match_by_name("WILSON DURNESS") is None


def test_a_corrupt_timestamp_is_not_treated_as_recent(ais_caches, monkeypatch):
    vessels, _ = ais_caches
    monkeypatch.setattr(ais, "AIS_MAX_AGE_MIN", 15)
    entry = _aged("WILSON DURNESS", minutes_ago=0)
    entry["last_seen"] = "not a timestamp"
    vessels["WILSON DURNESS"] = entry
    assert proxy.match_by_name("WILSON DURNESS") is None


def test_age_filter_narrows_the_pool_without_touching_the_cache(ais_caches, monkeypatch):
    """The point of excluding rather than purging: the data survives, so raising the
    threshold later brings the vessel back rather than needing it re-broadcast."""
    vessels, _ = ais_caches
    monkeypatch.setattr(ais, "AIS_MAX_AGE_MIN", 15)
    vessels["WILSON DURNESS"] = _aged("WILSON DURNESS", minutes_ago=60)
    assert proxy.match_by_name("WILSON DURNESS") is None
    assert "WILSON DURNESS" in vessels               # still there
    monkeypatch.setattr(ais, "AIS_MAX_AGE_MIN", 120)
    assert proxy.match_by_name("WILSON DURNESS")["name"] == "WILSON DURNESS"


def test_last_seen_rolls_forward_rather_than_recording_entry_time(ais_caches, monkeypatch):
    """The point of the field. A position that is refreshed must carry a fresh timestamp,
    or it says nothing the position did not already say."""
    vessels, _ = ais_caches
    monkeypatch.setattr(ais, "_now", lambda: "2026-08-06 10:00:00")
    ais._process_ais(_position("ANOUK"))
    assert vessels["ANOUK"]["last_seen"] == "2026-08-06 10:00:00"

    monkeypatch.setattr(ais, "_now", lambda: "2026-08-06 10:05:00")
    ais._process_ais(_position("ANOUK", lat=52.5))
    assert vessels["ANOUK"]["last_seen"] == "2026-08-06 10:05:00"
    assert vessels["ANOUK"]["latitude"] == 52.5


def test_a_message_with_no_mmsi_is_ignored(ais_caches):
    vessels, _ = ais_caches
    ais._process_ais({"MessageType": "ShipStaticData", "MetaData": {},
                        "Message": {"ShipStaticData": {"Name": "GHOST"}}})
    assert vessels == {}


def test_a_malformed_message_does_not_raise(ais_caches):
    """The feed is external; a shape change must not kill the websocket thread."""
    ais._process_ais({"MessageType": "PositionReport", "MetaData": {"MMSI": 1},
                        "Message": None})


# ---------------------------------------------------------------------------
# record(): the multi-source merge core
#
# One merge point for whatever provider saw the observation, keyed by MMSI so two ships
# sharing a name (17 duplicate-name groups in a live AISHub snapshot of the Maas approach)
# stop overwriting each other.
# ---------------------------------------------------------------------------

def test_record_admits_a_named_vessel_and_indexes_it_by_mmsi(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    ais.record({"mmsi": "244123456", "name": "ORASUND", "callsign": "PBZL",
                "latitude": 52.0, "longitude": 3.9}, source="test")

    assert "ORASUND" in ais._vessel_cache
    assert ais._mmsi_index["244123456"]["name"] == "ORASUND"
    assert ais._callsign_cache["PBZL"]["mmsi"] == "244123456"


def test_record_holds_a_position_until_a_name_arrives(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    ais.record({"mmsi": "244000111", "latitude": 51.9, "longitude": 4.0},
               source="test", observed_at=1000.0)
    assert ais._vessel_cache == {}
    assert "244000111" in ais._pending

    ais.record({"mmsi": "244000111", "name": "LATE NAME"},
               source="test", observed_at=1001.0)

    entry = ais._vessel_cache["LATE NAME"]
    assert entry["latitude"] == 51.9
    assert "244000111" not in ais._pending


def test_record_does_not_alias_two_ships_that_share_a_name(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    ais.record({"mmsi": "111111111", "name": "ALBATROS"}, source="test")
    ais.record({"mmsi": "222222222", "name": "ALBATROS"}, source="test")

    assert ais._mmsi_index["111111111"]["mmsi"] == "111111111"
    assert ais._mmsi_index["222222222"]["mmsi"] == "222222222"
    assert ais._mmsi_index["111111111"] is not ais._mmsi_index["222222222"]


def test_record_keeps_the_newer_position_when_an_older_one_arrives_late(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    ais.record({"mmsi": "244777888", "name": "NEWEST WINS",
                "latitude": 52.5, "longitude": 4.5}, source="a", observed_at=2000.0)
    ais.record({"mmsi": "244777888", "latitude": 51.0, "longitude": 3.0},
               source="b", observed_at=1000.0)

    assert ais._vessel_cache["NEWEST WINS"]["latitude"] == 52.5


def test_record_stamps_last_seen_from_the_observation_not_the_clock(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    import datetime as _dt
    observed = 1786528800.0        # 2026-08-12 10:00:00 UTC
    ais.record({"mmsi": "244999000", "name": "TIMESTAMPED",
                "latitude": 52.0, "longitude": 4.0},
               source="aishub", observed_at=observed)

    # Expectation computed, not hardcoded: last_seen is local wall-clock throughout this
    # codebase, so a literal would only pass in one timezone.
    expected = _dt.datetime.fromtimestamp(observed).strftime("%Y-%m-%d %H:%M:%S")
    assert ais._vessel_cache["TIMESTAMPED"]["last_seen"] == expected
    assert ais._vessel_cache["TIMESTAMPED"]["last_seen"] != ais._now()


def test_record_never_blanks_a_name_with_an_empty_string(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    ais.record({"mmsi": "244321000", "name": "KEEPS ITS NAME"}, source="test")
    ais.record({"mmsi": "244321000", "name": "", "latitude": 52.0,
                "longitude": 4.0}, source="test")

    assert ais._mmsi_index["244321000"]["name"] == "KEEPS ITS NAME"


# ---------------------------------------------------------------------------
# Feed silence detection
#
# On 2026-08-07 the feed was found delivering nothing for an entire session: the websocket
# connected, "[AIS] connected" printed, and then silence -- no error, no disconnect, no log
# line of any kind, while every lookup carried on matching happily against a cache last
# updated three days earlier. Two blind spots made that invisible, and both are covered here.


def test_a_frame_with_no_message_type_is_reported(ais_caches, capsys):
    """aisstream signals refusals as {"error": ...} on an otherwise healthy socket.

    Such a frame has no MessageType and no MMSI, so it used to hit the `if not mmsi: return`
    line and vanish without trace -- the single most diagnostic thing the feed can send,
    silently discarded.
    """
    ais._process_ais({"error": "Api Key Is Not Valid"})
    out = capsys.readouterr().out
    assert "Api Key Is Not Valid" in out, "the server's own error text must reach the log"


def test_unrecognised_frames_are_logged_but_rate_limited(ais_caches, capsys):
    """A persistent error must not turn into an unbounded log flood."""
    for _ in range(50):
        ais._process_ais({"error": "nope"})
    assert capsys.readouterr().out.count("nope") <= ais._UNKNOWN_FRAME_LOG_LIMIT


def test_a_recognised_frame_records_when_it_arrived(ais_caches, monkeypatch):
    monkeypatch.setattr(ais, "_last_message_at", None)
    ais._process_ais(_position("ANOUK"))
    assert ais._last_message_at is not None, "needed to tell 'quiet' from 'dead'"


def test_silence_report_is_quiet_while_data_flows():
    assert ais._silence_report(last_message_at=1000.0, connected_at=900.0,
                               now=1030.0, threshold=60) is None


def test_silence_report_warns_when_a_connected_feed_goes_quiet():
    msg = ais._silence_report(last_message_at=1000.0, connected_at=900.0,
                              now=1100.0, threshold=60)
    assert msg is not None and "100" in msg


def test_silence_report_warns_when_no_frame_ever_arrived():
    """The actual 2026-08-07 shape: connected, subscribed, never sent a single frame."""
    msg = ais._silence_report(last_message_at=None, connected_at=900.0,
                              now=1000.0, threshold=60)
    assert msg is not None
    assert "no data at all" in msg.lower(), "must read differently from a mid-stream stall"


def test_silence_report_can_be_disabled():
    assert ais._silence_report(last_message_at=None, connected_at=0.0,
                               now=1e9, threshold=0) is None


# ---------------------------------------------------------------------------
# Reconnect backoff
#
# On 2026-08-08 the 08-07 outage changed shape: aisstream stopped answering the client's
# keepalive pings, so the connection now dies ~40s in (ping_interval 20 + ping_timeout 20)
# with `1011 keepalive ping timeout` instead of sitting there silently. The retry was a flat
# `sleep(30)`, so we reconnected at a fixed rate forever -- and after two cycles aisstream
# answered `HTTP 429`. A dead upstream had been turned into us hammering it.
#
# The subtlety that makes a naive fix useless: every connection SUCCEEDS. Backoff keyed on
# connection failure would reset on each accept and never engage. It has to be keyed on the
# connection proving useful, which means delivering a frame.


def test_first_reconnect_is_prompt():
    """A one-off blip deserves a fast retry, not a punishment."""
    assert ais._reconnect_delay(0, jitter=0.0) == pytest.approx(ais._RECONNECT_BASE_SEC)


def test_reconnect_delay_grows_with_consecutive_failures():
    delays = [ais._reconnect_delay(n, jitter=0.0) for n in range(6)]
    assert delays == sorted(delays) and len(set(delays)) == len(delays), (
        f"consecutive failures must back off, got {delays}")


def test_reconnect_delay_is_capped():
    """Backing off must not become never coming back."""
    assert ais._reconnect_delay(99, jitter=1.0) <= ais._RECONNECT_CAP_SEC * 1.5


def test_rate_limited_reconnect_waits_at_least_a_minute():
    """HTTP 429 is the server saying *you specifically are too fast*; honour it."""
    assert ais._reconnect_delay(0, rate_limited=True, jitter=0.0) >= 60


def test_jitter_only_ever_adds_delay():
    """Jitter de-syncs retries; it must never push us back under the intended floor."""
    for n in (0, 3, 99):
        for r in (0.0, 0.5, 1.0):
            assert ais._reconnect_delay(n, jitter=r) >= ais._reconnect_delay(n, jitter=0.0)


def test_jitter_actually_spreads_the_delay():
    assert ais._reconnect_delay(3, jitter=0.0) != ais._reconnect_delay(3, jitter=1.0)


def test_rate_limit_is_recognised_from_the_exception():
    """websockets 16 raises InvalidStatus carrying the response; 429 is the one that matters."""
    from websockets.exceptions import InvalidStatus

    class _Resp:
        def __init__(self, code): self.status_code = code

    assert ais._is_rate_limited(InvalidStatus(_Resp(429))) is True
    assert ais._is_rate_limited(InvalidStatus(_Resp(503))) is False
    assert ais._is_rate_limited(RuntimeError("keepalive ping timeout")) is False


# --- the loop itself, driven against a fake websocket -----------------------


class _FakeAisSocket:
    """A socket that yields `frames` and then ends -- abruptly, or gracefully."""

    def __init__(self, frames, graceful=False):
        self._frames = list(frames)
        self._graceful = graceful
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return json.dumps(self._frames.pop(0))
        if self._graceful:
            raise StopAsyncIteration
        raise ConnectionError("keepalive ping timeout")


class _FakeAisConnect:
    def __init__(self, frames_per_connection, graceful=False):
        self._frames = list(frames_per_connection)
        self._graceful = graceful
        self.connections = []

    def __call__(self, *args, **kwargs):
        frames = self._frames.pop(0) if self._frames else []
        self._conn = _FakeAisSocket(frames, graceful=self._graceful)
        self.connections.append(self._conn)
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _StopLoop(Exception):
    pass


def _run_loop(monkeypatch, frames_per_connection, stop_after, graceful=False):
    """Drive `_ais_loop` against fakes; return the delays slept and the fake connector."""
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) >= stop_after:
            raise _StopLoop

    connect = _FakeAisConnect(frames_per_connection, graceful=graceful)
    monkeypatch.setattr(ais.websockets, "connect", connect)
    monkeypatch.setattr(ais, "_sleep", fake_sleep)
    monkeypatch.setattr(ais, "AIS_SILENCE_WARN_SEC", 0)

    try:
        asyncio.run(ais._ais_loop("test-key"))
    except _StopLoop:
        pass
    return delays, connect


def test_a_server_that_accepts_then_dies_makes_us_back_off(ais_caches, monkeypatch):
    """The exact 2026-08-08 fault. Every connection succeeds, so a fixed rate is the bug."""
    delays, _ = _run_loop(monkeypatch, frames_per_connection=[], stop_after=4)
    assert delays == sorted(delays) and len(set(delays)) == len(delays), (
        f"a repeatedly-dying feed must be retried more and more slowly, got {delays}")


def test_a_productive_connection_resets_the_backoff(ais_caches, monkeypatch):
    """Backoff must not accumulate across a feed that is actually working.

    Connections 1 and 2 deliver nothing, so the delay grows; connection 3 delivers a frame,
    so delays 3 and 4 must be back at the base. Compared against the base band rather than
    each other, since jitter makes every draw different by design.
    """
    good = [_position("ANOUK")]
    delays, _ = _run_loop(monkeypatch,
                          frames_per_connection=[[], [], good, []],
                          stop_after=4)
    base_band = (ais._RECONNECT_BASE_SEC, ais._RECONNECT_BASE_SEC * (1 + ais._RECONNECT_JITTER))
    assert delays[1] > base_band[1], f"a second dead connection must back off, got {delays}"
    for i in (2, 3):
        assert base_band[0] <= delays[i] <= base_band[1], (
            f"delay {i} should be back at the base after data arrived, got {delays}")


def test_a_gracefully_closing_server_is_still_paced(ais_caches, monkeypatch):
    """A clean close ends the `async for` without raising, so it used to skip the wait entirely.

    That is a hot reconnect loop against a server that is politely telling us to go away --
    the fastest possible way to earn the HTTP 429 seen on 2026-08-08.
    """
    delays, _ = _run_loop(monkeypatch, frames_per_connection=[], stop_after=3, graceful=True)
    assert len(delays) == 3 and all(d >= ais._RECONNECT_BASE_SEC for d in delays), (
        f"a clean close must be paced like any other, got {delays}")


def test_the_subscription_is_sent_on_every_reconnect(ais_caches, monkeypatch):
    """A reconnect that forgot to subscribe would recreate the 08-07 silent feed from our side."""
    _, connect = _run_loop(monkeypatch, frames_per_connection=[], stop_after=3)
    assert len(connect.connections) >= 3
    for conn in connect.connections:
        assert len(conn.sent) == 1, "each connection must send exactly one subscription"
        assert json.loads(conn.sent[0])["APIKey"] == "test-key"


# ---------------------------------------------------------------------------
# AIS hint filtering
#
# The original settings (WRatio, cutoff 65, 3-char tokens) produced 1,993 distinct spurious
# probe->vessel pairs over 307 real transcripts, because WRatio partial-matches a short word
# into any long name containing it. Those hints were then offered to Claude as evidence.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("probe", [
    "GOOD DAY",      # -> GOOD WAY (88 under WRatio): the reported false identification
    "GOOD MORNING",
    "SEVEN",         # -> STEVEN
    "ECHO",          # phonetic alphabet, not a vessel here
    "STARBOARD",
])
def test_all_stopword_phrases_are_not_probed(probe):
    """The guard's job: a phrase made entirely of ordinary speech is never looked up."""
    assert probe not in proxy._hint_probes(probe)


def test_mixed_phrases_are_probed_but_stopped_by_the_scorer(ais_cache):
    """'THE FOOT' contains a non-stopword so the guard passes it, and that is fine --
    fuzz.ratio is what stops it, where the old WRatio scored it 86 against
    'THE QUEEN JACQUELINE'. Both layers matter; this pins the second one."""
    assert "THE FOOT" in proxy._hint_probes("walking on the foot area")
    assert proxy._find_ais_hints("walking on the foot area") == []


@pytest.mark.parametrize("text,expected", [
    ("WILSON DURNESS calling", "WILSON DURNESS"),
    ("this is MSC PANTERA", "MSC PANTERA"),
    ("Motortanker NEPTUNE here", "NEPTUNE"),
])
def test_real_vessel_names_are_still_probed(text, expected):
    assert expected in proxy._hint_probes(text)


def test_probe_guard_needs_every_token_to_be_common():
    """'GOOD WAY' must survive even though 'GOOD' alone is a stopword -- otherwise a real
    vessel whose name contains a common word could never be hinted."""
    assert "GOOD WAY" in proxy._hint_probes("GOOD WAY calling")


@pytest.mark.parametrize("text,pair", [
    ("GOOD WAY calling", "GOOD WAY"),      # second word is 3 chars
    ("this is NQ TULIPA", "NQ TULIPA"),    # first word is 2 chars
])
def test_pairs_survive_when_only_one_token_is_substantial(text, pair):
    """Requiring *both* tokens to clear the length bar silently dropped real vessel
    names -- a recall regression this pins down."""
    assert pair in proxy._hint_probes(text)


def test_short_tokens_are_not_probed():
    assert "THE" not in proxy._hint_probes("THE")


# Longer spans
#
# The matcher scores whole strings, so a name longer than the longest probe is unreachable
# at every probe length that exists -- while one of its words can match a different, real
# vessel outright. Adjacent pairs alone lost every three-word name.

@pytest.mark.parametrize("text,expected", [
    ("this is SANTA ISABEL MAERSK", "SANTA ISABEL MAERSK"),
    ("Maas Approach, MSC MARIA PIA", "MSC MARIA PIA"),
])
def test_three_word_names_are_probed_whole(text, expected):
    assert expected in proxy._hint_probes(text)


def test_spans_run_to_four_words_and_stop():
    # Deliberately not NATO phonetics -- those are all stopwords, so a span of them is
    # filtered before length ever comes into it.
    probes = proxy._hint_probes("STOLT GREENSHANK BALTIC SPLIT ORASUND")
    assert "STOLT GREENSHANK BALTIC SPLIT" in probes
    assert not any(len(p.split()) > 4 for p in probes)


def test_span_length_is_configurable(monkeypatch):
    monkeypatch.setattr(ais, "AIS_HINT_MAX_NGRAM", 2)
    probes = proxy._hint_probes("SANTA ISABEL MAERSK calling")
    assert "SANTA ISABEL" in probes
    assert "SANTA ISABEL MAERSK" not in probes


def test_stopword_guard_still_applies_to_longer_spans():
    """A four-word span of pure speech is no more worth looking up than a two-word one."""
    probes = proxy._hint_probes("good morning please thank sir madam")
    assert not any(len(p.split()) >= 3 for p in probes), probes


def test_longer_spans_do_not_crowd_the_right_vessel_out(ais_cache):
    """The failure mode worth guarding: more probes means more matches, and the hint list
    holds only five. A vessel that was hinted before must still be hinted now."""
    text = "Maas Approach, this is WILSON DURNESS, over."
    names = [h["name"] for h in proxy._find_ais_hints(text)]
    assert "WILSON DURNESS" in names


@pytest.fixture
def ais_cache(monkeypatch):
    cache = {
        "GOOD WAY":       {"name": "GOOD WAY", "mmsi": "538010145"},
        "WILSON DURNESS": {"name": "WILSON DURNESS", "mmsi": "314632000"},
        "SYNTHESE 11":    {"name": "SYNTHESE 11", "mmsi": "111111111"},
        "AFTER YOU":      {"name": "AFTER YOU", "mmsi": "222222222"},
    }
    monkeypatch.setattr(ais, "_vessel_cache", cache)
    return cache


def test_hints_no_longer_surface_a_vessel_from_a_greeting(ais_cache):
    """The whole reported bug in one assertion."""
    assert proxy._find_ais_hints("Yes, good day sir, we are entering new area") == []


def test_hints_still_surface_a_stated_vessel(ais_cache):
    hits = proxy._find_ais_hints("Maas Approach, Wilson Durness, calling you")
    assert [h["name"] for h in hits] == ["WILSON DURNESS"]


def test_hint_filter_can_be_disabled(monkeypatch, ais_cache):
    """AIS_HINT_FILTER=off must restore the old loose behaviour exactly, which is what
    makes the revert trustworthy."""
    monkeypatch.setattr(ais, "AIS_HINT_FILTER", False)
    assert proxy._find_ais_hints("Yes, good day sir, we are entering new area") != []


def _legacy_probes(text: str) -> list[str]:
    """The probe generation exactly as it was before this change, for equivalence checks."""
    words = text.upper().split()
    probes = []
    for i, w in enumerate(words):
        if len(w) >= 3:
            probes.append(w)
        if i < len(words) - 1 and len(words[i + 1]) >= 3:
            probes.append(f"{w} {words[i + 1]}")
    return probes


@pytest.mark.parametrize("text", [
    "Yes, good day sir, we are entering new area, over.",
    "Maas Approach, Maas Approach, Wilson Durness, calling you.",
    "Callsign Juliet Lima Sierra Romeo, this is NQ TULIPA.",
    "",
    "a",
])
def test_flag_off_reproduces_the_original_probe_generation(monkeypatch, text):
    """The revert has to be exact, not approximate: with the flag off this must produce
    byte-identical probes to the pre-change implementation."""
    monkeypatch.setattr(ais, "AIS_HINT_FILTER", False)
    assert proxy._hint_probes(text) == _legacy_probes(text)


# ---------------------------------------------------------------------------
# Vessel name matching
#
# match_by_name kept the WRatio scorer after _find_ais_hints was moved off it, and hit the
# same substring failure one layer further down: WRatio falls back to partial_ratio*0.9 when
# the strings differ in length by 1.5x-8x, so a 2-letter vessel name scores 90 against any
# longer name containing it. Measured over the live cache, that is 15.1% wrong matches.
# ---------------------------------------------------------------------------

@pytest.fixture
def name_cache(monkeypatch):
    """The cache as it stood when 'RA' was reported, plus a short name worth keeping."""
    cache = {
        "ORASUND": {"name": "ORASUND", "mmsi": "220514000", "callsign": "OXBU2"},
        "RA":      {"name": "RA",      "mmsi": "244729064", "callsign": ""},
        "AMY":     {"name": "AMY",     "mmsi": "244710116", "callsign": "PD2759"},
        "NEPTUNE": {"name": "NEPTUNE", "mmsi": "205105000", "callsign": ""},
    }
    monkeypatch.setattr(ais, "_vessel_cache", cache)
    return cache


def test_mishearing_matches_the_vessel_not_a_short_name_inside_it(name_cache):
    """The whole reported bug in one assertion: 'Motortanker Orason' was identified as RA
    (MMSI 244729064) because 'RA' is a substring of o-RA-son, scoring 90 to ORASUND's 77."""
    assert proxy.match_by_name("ORASON")["name"] == "ORASUND"


@pytest.mark.parametrize("heard", ["ORASON", "ORASUN", "ORA SUND", "MOTORTANKER ORASON"])
def test_orasund_survives_the_ways_it_gets_misheard(heard, name_cache):
    assert proxy.match_by_name(heard)["name"] == "ORASUND"


@pytest.mark.parametrize("heard", ["ORASON", "MARATHON", "GRACE", "RADAR"])
def test_a_two_letter_name_is_never_reached_by_partial_match(heard, name_cache):
    """'RA' must only match someone who actually said RA."""
    got = proxy.match_by_name(heard)
    assert got is None or got["name"] != "RA"


@pytest.mark.parametrize("heard", ["RA", "ra", "AMY", "amy"])
def test_short_names_still_match_when_actually_said(heard, name_cache):
    """The guard is equality-based, not a ban: short vessels are real and do call in."""
    assert proxy.match_by_name(heard)["name"] == heard.upper()


def test_name_match_falls_back_to_word_windows(name_cache):
    """The SKIP-word fallback path still finds a name buried in a longer phrase."""
    assert proxy.match_by_name("MOTORTANKER NEPTUNE")["name"] == "NEPTUNE"


def test_name_match_returns_nothing_for_ordinary_speech(name_cache):
    assert proxy.match_by_name("YES GOOD DAY SIR") is None


def test_name_filter_can_be_disabled(monkeypatch, name_cache):
    """AIS_NAME_FILTER=off restores the old WRatio behaviour exactly -- including the bug,
    which is what makes the revert trustworthy."""
    monkeypatch.setattr(ais, "AIS_NAME_FILTER", False)
    assert proxy.match_by_name("ORASON")["name"] == "RA"


# ---------------------------------------------------------------------------
# Prompt echo
# ---------------------------------------------------------------------------

# Pinned to the prompt that shipped until 2026-08-06, not to whatever is current. These
# cases are the *reported incident* -- the CH01 exchange that came back as three different
# vessels -- and they must keep passing as documentation of the mechanism no matter how the
# shipped prompt is later reworded. Coverage of the current prompt is separate, below.
_PROMPT = (
    "Maas Approach, this is Motortanker Neptune, callsign PABC, requesting permission "
    "to enter the Botlek, over. "
    "Motortanker Neptune, Maas Approach, roger, proceed to VHF channel six one, out. "
    "Rotterdam VTS, be advised we are standing by on channel one six, over."
)


@pytest.mark.parametrize("text", [
    "Motortanker Neptune, Maas Approach.",                                   # the reported case
    "Motortanker Neptune, over.",
    "Motortanker Neptune, Maas Approach, roger.",
    "Rotterdam VTS, be advised we are standing by on channel one six, over.",
    "Motortanker Neptune, be advised we are standing by on channel one six, over.",
])
def test_prompt_echo_is_detected(text):
    assert proxy._is_prompt_echo(text, _PROMPT) is True


@pytest.mark.parametrize("text", [
    "Maas Approach, Maas Approach, Wilson Durness, calling you.",
    "Yes, good day sir, we are entering new area.",
    "This is Maas Approach.",          # every word is in the prompt, but nothing distinctive
    "VHF channel six, over.",
    "Over, Maas Approach, over.",
    "Maas Approach.",
])
def test_real_speech_is_not_flagged_as_echo(text):
    assert proxy._is_prompt_echo(text, _PROMPT) is False


def test_one_novel_word_is_enough_to_clear_a_transmission():
    """A word the prompt cannot supply means the speaker said something real."""
    assert proxy._is_prompt_echo("Motortanker Neptune, Maas Approach.", _PROMPT) is True
    assert proxy._is_prompt_echo("Motortanker Neptune, Maas Approach, Botlek bound.", _PROMPT) is False


# The shipped prompt, whatever it currently is, must still be echo-detectable. Without this
# a future prompt could quietly disable the filter -- it only fires on words the prompt
# actually contains, so a reworded prompt with none of the old vocabulary would silently
# stop suppressing anything.

def test_shipped_prompt_echo_is_detected():
    """A verbatim sentence of the live prompt, read back instead of transcribed."""
    first_sentence = proxy.DEFAULT_MARITIME_PROMPT.split(". ")[0] + "."
    assert proxy._is_prompt_echo(first_sentence, proxy.DEFAULT_MARITIME_PROMPT) is True


def test_shipped_prompt_leaves_real_speech_alone():
    assert proxy._is_prompt_echo("Maas Approach, Wilson Durness, calling you.",
                                 proxy.DEFAULT_MARITIME_PROMPT) is False


def test_prompt_echo_filter_can_be_disabled(monkeypatch):
    monkeypatch.setattr(corrections, "PROMPT_ECHO_FILTER", False)
    assert proxy._is_prompt_echo("Motortanker Neptune, over.", _PROMPT) is False


def test_prompt_echo_handles_empty_input():
    assert proxy._is_prompt_echo("", _PROMPT) is False
    assert proxy._is_prompt_echo("anything", "") is False


# ---------------------------------------------------------------------------
# Conversation sessions
# ---------------------------------------------------------------------------

def _turn(seconds_ago, vessel=None, text="", shore=False, channel="160,650"):
    return {
        "time": datetime.datetime.now() - datetime.timedelta(seconds=seconds_ago),
        "vessel": vessel, "raw_text": text, "shore": shore,
        "channel": channel, "fuzzy": False, "result": {},
    }


@pytest.fixture
def buffer(monkeypatch):
    entries = []
    monkeypatch.setattr(conversations, "_vessel_buffer", entries)
    return entries


# ---------------------------------------------------------------------------
# Conversation windowing
# ---------------------------------------------------------------------------

def _chunk(seconds_ago, text="", channel="160,650", cid=None, callsign=None, vessel=None,
           live_mmsi=None):
    return {
        "id": cid if cid is not None else seconds_ago,
        "time": datetime.datetime.now() - datetime.timedelta(seconds=seconds_ago),
        "channel": channel, "text": text, "callsign": callsign,
        "live_vessel": vessel, "live_mmsi": live_mmsi,
    }


@pytest.fixture
def journal(monkeypatch):
    entries = []
    monkeypatch.setattr(conversations, "_conversation_chunks", entries)
    return entries


def test_split_windows_breaks_on_a_long_silence():
    windows = proxy._split_windows([_chunk(300), _chunk(200), _chunk(20), _chunk(10)])
    assert [len(w) for w in windows] == [1, 1, 2]


def test_split_windows_keeps_a_continuous_exchange_together():
    windows = proxy._split_windows([_chunk(50), _chunk(40), _chunk(30), _chunk(20)])
    assert len(windows) == 1


def test_split_windows_caps_window_size(monkeypatch):
    """Bounds the resolver prompt: a busy channel must not build one huge window."""
    monkeypatch.setattr(conversations, "CONVERSATION_MAX_CHUNKS", 3)
    windows = proxy._split_windows([_chunk(60 - i, cid=i) for i in range(7)])
    assert [len(w) for w in windows] == [3, 3, 1]


def test_open_window_is_left_in_the_journal(journal):
    journal.extend([_chunk(20), _chunk(5)])
    assert proxy._take_closed_windows() == []
    assert len(journal) == 2, "an exchange still in progress must not be resolved early"


def test_quiet_window_is_taken(journal):
    journal.extend([_chunk(300), _chunk(290)])
    taken = proxy._take_closed_windows()
    assert [len(w) for w in taken] == [2]
    assert journal == []


def test_superseded_window_is_taken_but_the_live_one_is_kept(journal):
    journal.extend([_chunk(400, cid=1), _chunk(390, cid=2), _chunk(10, cid=3)])
    taken = proxy._take_closed_windows()
    assert [[c["id"] for c in w] for w in taken] == [[1, 2]]
    assert [c["id"] for c in journal] == [3]


def test_windows_do_not_span_channels(journal):
    journal.extend([_chunk(300, channel="160,650", cid=1), _chunk(299, channel="161,650", cid=2)])
    taken = proxy._take_closed_windows()
    assert sorted(len(w) for w in taken) == [1, 1]


def test_record_chunk_journals_the_raw_transcription(journal):
    proxy._record_chunk("160,650", "Mass Approach, Serenada.",
                        {"vessel": "SERENADA", "callsign": "PABC", "text": "Maas Approach, Serenada."})
    assert journal[0]["text"] == "Mass Approach, Serenada.", "resolver needs the raw decode"
    assert journal[0]["corrected"] == "Maas Approach, Serenada.", "page shows what the operator saw"
    assert journal[0]["live_vessel"] == "SERENADA"
    assert journal[0]["callsign"] == "PABC"


def test_record_chunk_falls_back_to_raw_when_uncorrected(journal):
    proxy._record_chunk("160,650", "Roger, over.", {"vessel": None})
    assert journal[0]["corrected"] == "Roger, over."


# ---------------------------------------------------------------------------
# Retrospective resolver
#
# The reason this design replaced forward context: its schema has no text field, so it
# cannot rewrite a transcription. That is asserted here rather than assumed.
# ---------------------------------------------------------------------------

_CANDIDATES = {
    "SERENADA": {"name": "SERENADA", "mmsi": "275545000", "callsign": "PABC", "type": 80},
    "WILSON DURNESS": {"name": "WILSON DURNESS", "mmsi": "314632000"},
}


def test_resolver_schema_has_no_text_field():
    """The firewall. If a text field ever appears here, the fabrication bug is back."""
    assert '"text"' not in proxy.RESOLVER_SYSTEM_PROMPT
    assert "Do NOT return transcriptions" in proxy.RESOLVER_SYSTEM_PROMPT


def test_validate_keeps_only_candidate_vessels():
    """A name outside the candidate list is dropped, not trusted -- free-form naming is how
    ordinary speech became real ships before the hint filter was tightened."""
    chunks = [_chunk(30, cid=1), _chunk(20, cid=2)]
    out = proxy._validate_exchanges(
        [{"chunk_ids": [1, 2], "vessel": "GOOD WAY", "confidence": "high"}], chunks, _CANDIDATES)
    assert out[0]["vessel"] is None


def test_validate_accepts_a_candidate_and_attaches_its_ais_detail():
    chunks = [_chunk(30, cid=1)]
    out = proxy._validate_exchanges(
        [{"chunk_ids": [1], "vessel": "serenada", "confidence": "high"}], chunks, _CANDIDATES)
    assert out[0]["vessel"] == "SERENADA"
    assert out[0]["mmsi"] == "275545000"


def test_validate_accounts_for_every_transmission():
    """No transmission may be silently dropped by the resolver."""
    chunks = [_chunk(30, cid=1), _chunk(20, cid=2), _chunk(10, cid=3)]
    out = proxy._validate_exchanges([{"chunk_ids": [1], "vessel": None}], chunks, _CANDIDATES)
    assert sorted(i for ex in out for i in ex["chunk_ids"]) == [1, 2, 3]


def test_validate_ignores_unknown_and_duplicate_chunk_ids():
    chunks = [_chunk(30, cid=1), _chunk(20, cid=2)]
    out = proxy._validate_exchanges(
        [{"chunk_ids": [1, 99]}, {"chunk_ids": [1, 2]}], chunks, _CANDIDATES)
    assert sorted(i for ex in out for i in ex["chunk_ids"]) == [1, 2]


@pytest.mark.parametrize("bad", [None, "not a list", [], [{"chunk_ids": []}]])
def test_validate_survives_a_malformed_response(bad):
    chunks = [_chunk(30, cid=1)]
    out = proxy._validate_exchanges(bad, chunks, _CANDIDATES)
    assert [i for ex in out for i in ex["chunk_ids"]] == [1]


def test_unresolved_fallback_keeps_every_chunk():
    out = proxy._unresolved([_chunk(30, cid=1), _chunk(20, cid=2)])
    assert out[0]["chunk_ids"] == [1, 2] and out[0]["vessel"] is None


def test_confidence_is_clamped_to_known_values():
    chunks = [_chunk(30, cid=1)]
    out = proxy._validate_exchanges(
        [{"chunk_ids": [1], "vessel": None, "confidence": "certain"}], chunks, _CANDIDATES)
    assert out[0]["confidence"] == "low"


def test_callsign_candidates_are_marked_and_come_first(monkeypatch):
    """An exact callsign lookup is evidence, not similarity, so the resolver is told so."""
    monkeypatch.setattr(ais, "_callsign_cache", {"PABC": _CANDIDATES["SERENADA"]})
    monkeypatch.setattr(ais, "_vessel_cache", {})
    cands = proxy._resolver_candidates([_chunk(10, "callsign papa alpha bravo charlie", callsign="PABC")])
    assert cands[0]["name"] == "SERENADA" and cands[0]["via_callsign"] is True


@pytest.mark.parametrize("callsign,text", [
    # Real transmissions from the 07-28 replay that must keep working.
    ("5LKV5", "Maas Approach, this is MSC Athens, Callsign five Lima Kilo Victor Five."),
    ("JLSR", "Callsign Juliet Lima Sierra Romeo, over."),
    ("9HA6176", "Maas Approach, Cigars Lay Apart, Callsign 9 Hotel Alpha six one seven six"),
    ("9HF5093", "this is motor vessel Anna, callsign 9HF5093, over"),   # verbatim
    ("ZCF7", "Callsign Zulu Charlie Foxtrot seven, over"),
])
def test_a_spelled_out_callsign_is_supported(callsign, text):
    assert proxy._callsign_supported_by_text(callsign, text) is True


@pytest.mark.parametrize("callsign,text", [
    # Every one of these was actually produced by the live pass on the 07-28 captures.
    ("VRSQ4", "Gungor Star one three one five, correct."),
    ("PE2026", "Help Trader Maas Approach."),
    ("PA3534", "MSC Jungair, MSC Jungair, this is Mildredship Protector on one."),
    ("PE2026", "Maas Approach, this is Masiadel, Masiadel, Maas Approach."),
    ("PABC", "Maas Approach, Maas Approach, Wilson Durness, calling you."),
])
def test_an_invented_callsign_is_rejected(callsign, text):
    assert proxy._callsign_supported_by_text(callsign, text) is False


@pytest.mark.parametrize("callsign,text", [
    ("", "Callsign Juliet Lima Sierra Romeo"),
    (None, "Callsign Juliet Lima Sierra Romeo"),
    ("AB", "Alpha Bravo"),          # too short to be distinctive
    ("JLSR", ""),
    ("JLSR", None),
])
def test_callsign_support_handles_edge_cases(callsign, text):
    assert proxy._callsign_supported_by_text(callsign, text) is False


def test_spelled_out_runs_break_on_ordinary_words():
    """Runs must not bridge unrelated speech, or scattered digits would form a callsign."""
    runs = proxy._spelled_out_runs("Alpha Bravo proceeding Charlie Delta")
    assert runs == ["AB", "CD"]


# A phonetic word the table does not know does not merely fail to decode -- it breaks the
# run in half, so the letters either side are lost too. Both spellings below are real: "Gulf"
# for Golf is how the letter is widely said on an international channel, and "X-ray" is the
# ordinary written form. Each cost a full identification.
@pytest.mark.parametrize("callsign,text,expected_runs", [
    # MONA SWAN (MMSI 219624000, cs OWGJ2) went unidentified: runs were ['OW', 'J2'].
    ("OWGJ2", "confirm your Callsign Oscar Whiskey Gulf Juliet two.", ["OWGJ2"]),
    # From the 07-28 reference corpus, ground truth: runs were ['PBU', '1'].
    ("PBUX", "this is Motor vessel Alaskaborg, Callsign, Papa, Bravo, Uniform, X-ray, "
             "calling in channel one", None),
])
def test_alternate_phonetic_spellings_do_not_split_the_run(callsign, text, expected_runs):
    assert proxy._callsign_supported_by_text(callsign, text) is True
    if expected_runs:
        assert proxy._spelled_out_runs(text) == expected_runs


def test_golf_and_gulf_decode_alike():
    assert (proxy._spelled_out_runs("Oscar Whiskey Golf Juliet two")
            == proxy._spelled_out_runs("Oscar Whiskey Gulf Juliet two")
            == ["OWGJ2"])


@pytest.mark.parametrize("text", [
    "we are in the Gulf of Mexico, over",           # the ordinary word, alone
    "proceeding to the Persian Gulf next week",
])
def test_gulf_as_an_ordinary_word_still_yields_no_callsign(text):
    """Adding a homophone widens the decoder, so pin that it cannot manufacture one: a
    lone letter is not a callsign, and the length floor is what stops it."""
    assert proxy._callsign_supported_by_text("G", text) is False
    assert all(len(run) < 3 for run in proxy._spelled_out_runs(text))


@pytest.mark.parametrize("placeholder", ["null", "None", "N/A", "unknown", "-", " ", ""])
def test_placeholder_strings_become_real_nulls(placeholder):
    """The schema asks for "<name or null>" and the model sometimes sends the word instead.

    A string is truthy, so it survives every `or` fallback downstream and reaches the plugin
    as a real value -- "[GH NIGHTINGALE/null]" -- and a vessel named "unknown" would be looked
    up against AIS.
    """
    result = {"vessel": placeholder, "callsign": placeholder,
              "vessel_type": placeholder, "text": "Maas Approach."}
    identify._null_out_placeholders(result)
    assert result["vessel"] is None
    assert result["callsign"] is None
    assert result["vessel_type"] is None
    assert result["text"] == "Maas Approach.", "text must never be nulled by this pass"


def test_real_values_survive_the_placeholder_pass():
    result = {"vessel": "MSC Athens", "callsign": "5LKV5", "vessel_type": "container"}
    identify._null_out_placeholders(result)
    assert result == {"vessel": "MSC Athens", "callsign": "5LKV5", "vessel_type": "container"}


def test_partial_callsign_pattern_decodes_the_real_transmission():
    """MSC TEMA VIII (5LRK9) went unidentified: Whisper heard Lima->'DEMA', Kilo->'clear'."""
    text = ("Good afternoon, this is Motortanker MSC DEMA eight, "
            "Callsign five DEMA Romeo, clear nine.")
    assert proxy._partial_callsign_pattern(text) == ("5.R.9", 3)


def test_partial_callsign_pattern_is_anchored_on_the_keyword():
    """Unanchored, 'eight' from the vessel name leaks in and yields '8.5.R.9'."""
    assert proxy._partial_callsign_pattern(
        "Motortanker MSC DEMA eight, five DEMA Romeo, clear nine") is None


# Each case must be rejected by the rule it names and no other, or it proves nothing.
# "Callsign Oscar Whiskey" for instance is fully decoded, so it would be refused by the
# all-decoded branch long before the minimum-known rule ever ran.
@pytest.mark.parametrize("text,rejected_by", [
    ("Callsign Oscar dema Whiskey",                     "2 known characters, floor is 3"),
    ("Callsign five dema clear kilos Romeo nine",       "3 consecutive wildcards, max is 2"),
    ("Callsign one two three dema four five six seven", "8 characters, ITU max is 7"),
    ("Callsign Zulu Charlie Foxtrot seven, over",       "fully decoded: the exact path owns it"),
    ("Maas Approach, Wilson Durness calling",           "no callsign keyword"),
    ("", "empty"),
    (None, "None"),
])
def test_partial_callsign_pattern_rejects_what_it_cannot_use(text, rejected_by):
    assert proxy._partial_callsign_pattern(text) is None, rejected_by


def test_partial_callsign_pattern_counts_one_wildcard_per_unreadable_word():
    got, known = proxy._partial_callsign_pattern("Callsign five dema Romeo clear nine")
    assert got == "5.R.9" and known == 3


@pytest.fixture
def pattern_cache(monkeypatch):
    cache = {
        "5LRK9": {"name": "MSC TEMA VIII", "mmsi": "636024193", "callsign": "5LRK9"},
        "5LCP9": {"name": "SIKINOS",       "mmsi": "111111111", "callsign": "5LCP9"},
        "PABC":  {"name": "SERENADA",      "mmsi": "275545000", "callsign": "PABC"},
    }
    monkeypatch.setattr(ais, "_callsign_cache", cache)
    return cache


def test_pattern_match_returns_the_only_vessel_that_fits(pattern_cache):
    assert proxy.match_by_callsign_pattern("5.R.9")["name"] == "MSC TEMA VIII"


def test_pattern_match_refuses_when_several_vessels_fit(pattern_cache):
    """Ambiguity is not an identification -- 5L..9 fits both 5LRK9 and 5LCP9."""
    assert proxy.match_by_callsign_pattern("5L..9") is None


@pytest.mark.parametrize("pattern", ["9.Z.4", "", None, "["])
def test_pattern_match_returns_nothing_when_it_cannot_match(pattern, pattern_cache):
    """Includes a malformed pattern: never raise into the resolver."""
    assert proxy.match_by_callsign_pattern(pattern) is None


def test_pattern_match_is_anchored_at_both_ends(pattern_cache):
    """'PAB' must not match 'PABC' -- a callsign is matched whole or not at all."""
    assert proxy.match_by_callsign_pattern("PAB") is None


# ---------------------------------------------------------------------------
# Phonetic-run callsign anchor
#
# From the BERGE TOWNSEND conversation of 2026-08-07 10:17:50, root-caused in full. The
# correct ship was in the cache the whole time and two independent paths to it failed:
#
#   - The callsign path never ran. `_CALLSIGN_ANCHOR_RE` needs the literal word "callsign",
#     but it was transcribed "call time two" and "all time two", so the anchor was False on
#     all three turns and the spelled-out characters were never even looked at.
#   - "Papa Bravo 8" was spoken twice. Callsigns CONTAINING PB8 = 79 cached vessels, useless.
#     Callsigns ENDING in PB8 = exactly 1, 2FPB8, BERGE TOWNSEND. Verified against the live
#     cache on 2026-08-08: 8,008 callsigns, 79 containing, 1 ending.
#
# So: anchor on the phonetic run itself rather than a keyword that STT can eat, and match
# tail-anchored. A 3-character tail is unique for only 23% of the cache, which is why
# ambiguity must return None and why the name-corroboration gate stays in front of this.


def test_phonetic_runs_need_no_callsign_keyword():
    """The whole point: 'callsign' was heard as 'call time two' and the path died there.

    The run is "2PB8", not "PB8": the "two" of "all time two" is the callsign's own leading
    2, and the Foxtrot between it and the Papa was lost to noise.
    """
    assert proxy._phonetic_callsign_probes("all time two, Papa Bravo 8") == ["2PB8"]


def test_the_run_boundary_is_what_isolates_the_callsign():
    """Why no tail-peeling is needed: the words that broke the callsign also end the run.

    "backstreet" sits between the "two" and the "Papa", so the run is PB8 -- already a tail
    of exactly one cached callsign. Peeling was implemented, measured over 218 conversations,
    found to add one false positive and zero true positives, and removed.
    """
    assert proxy._phonetic_callsign_probes(
        "Berkey Fountain, call time two, backstreet Papa Bravo 8, calling you over.") == ["PB8"]


def test_phonetic_runs_ignore_ordinary_speech():
    """Ordinary words must not be assembled into a callsign probe."""
    assert proxy._phonetic_callsign_probes("we have the Townsend proceeding inbound") == []


def test_phonetic_runs_require_real_phonetic_letters():
    """Spoken digits alone are a channel number or a time, not a callsign."""
    assert proxy._phonetic_callsign_probes("switch to one six all the way") == []


def test_phonetic_runs_reject_a_single_letter():
    """One phonetic word plus digits does not discriminate; two letters is the floor."""
    assert proxy._phonetic_callsign_probes("Papa 8 8") == []


def test_phonetic_runs_break_on_ordinary_words():
    """'Papa Bravo' and 'Charlie Delta' are two probes, not one six-character run."""
    probes = proxy._phonetic_callsign_probes("Papa Bravo 8 inbound Charlie Delta 4")
    assert "PB8" in probes and "CD4" in probes
    assert not any(len(p) > 3 for p in probes)


def test_suffix_match_finds_the_one_vessel_whose_callsign_ends_that_way(pattern_cache):
    """5LRK9 and 5LCP9 both contain 'K9'-ish noise; only one ends 'RK9'."""
    assert proxy.match_by_callsign_suffix("RK9")["name"] == "MSC TEMA VIII"


def test_suffix_match_refuses_when_several_vessels_fit(monkeypatch):
    """Ambiguity is not an identification, even when the tail is long enough to be checked."""
    monkeypatch.setattr(ais, "_callsign_cache", {
        "2FPB8": {"name": "BERGE TOWNSEND", "mmsi": "1", "callsign": "2FPB8"},
        "9XPB8": {"name": "SOMEONE ELSE",   "mmsi": "2", "callsign": "9XPB8"},
    })
    assert proxy.match_by_callsign_suffix("PB8") is None


def test_suffix_match_is_a_tail_not_a_substring(pattern_cache):
    """The 79-vs-1 distinction. 'PAB' is inside PABC but PABC does not end with it."""
    assert proxy.match_by_callsign_suffix("PAB") is None


@pytest.mark.parametrize("probe", ["", None, "XY"])
def test_suffix_match_returns_nothing_when_it_cannot_match(probe, pattern_cache):
    assert proxy.match_by_callsign_suffix(probe) is None


def test_the_berge_townsend_conversation_now_resolves(monkeypatch):
    """The end-to-end regression: the real transcript, the real callsign, the real cache entry.

    Both gates must pass -- the phonetic run gives the callsign tail, and the shore station
    saying 'Townsend' corroborates the name. Neither alone may identify a ship.
    """
    monkeypatch.setattr(ais, "_callsign_cache", {
        "2FPB8": {"name": "BERGE TOWNSEND", "mmsi": "235093069", "callsign": "2FPB8"},
        "FM6432": {"name": "VISION", "mmsi": "226003310", "callsign": "FM6432"},
    })
    monkeypatch.setattr(ais, "_vessel_cache", {})
    # Verbatim decoder output for clips 0003/0004/0005 of the 2026-08-07 capture. Not
    # paraphrased: "call time two" and "Bergy Township" are what the mis-hearing actually
    # looks like, and a cleaned-up version of it would not exercise this path.
    chunks = [
        _chunk(10, "Maaas Approach, Maaas Approach, motor vision, Berkey Fountain, "
                   "call time two, backstreet Papa Bravo 8, calling you over."),
        _chunk(20, "Mahaas Aproach, Mahaas Aproach, Otterbeesel, Bergy Township, "
                   "all time two, Pax Trat, Papa Bravo Eight, pulling over."),
        _chunk(30, "We have the Townsend Maaas approach, good morning."),
    ]
    found = proxy._partial_callsign_candidates(chunks)
    assert "235093069" in found, "BERGE TOWNSEND was in the cache the whole time"
    assert found["235093069"]["name"] == "BERGE TOWNSEND"


def test_the_phonetic_anchor_can_be_switched_off_for_an_ab(monkeypatch):
    """It has to be isolatable, or --resolve measures every change since the stored verdicts."""
    monkeypatch.setattr(ais, "_callsign_cache", {
        "2FPB8": {"name": "BERGE TOWNSEND", "mmsi": "235093069", "callsign": "2FPB8"}})
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(conversations, "AIS_PHONETIC_CALLSIGN", False)
    chunks = [
        _chunk(10, "Maaas Approach, motor vision, call time two, backstreet Papa Bravo 8."),
        _chunk(20, "We have the Townsend Maaas approach, good morning."),
    ]
    assert proxy._partial_callsign_candidates(chunks) == {}


def test_a_phonetic_run_alone_does_not_identify_a_ship(monkeypatch):
    """Without the name spoken, a unique tail is still only a guess wearing evidence's clothes."""
    monkeypatch.setattr(ais, "_callsign_cache", {
        "2FPB8": {"name": "BERGE TOWNSEND", "mmsi": "235093069", "callsign": "2FPB8"}})
    monkeypatch.setattr(ais, "_vessel_cache", {})
    chunks = [_chunk(10, "Maaas Approach, Maaas Approach, motor vision, call time two, "
                         "backstreet Papa Bravo 8, calling you over.")]
    assert proxy._partial_callsign_candidates(chunks) == {}


def test_invented_callsigns_are_not_promoted_to_evidence(monkeypatch):
    """Measured: the live pass emitted callsigns for transmissions containing none, and they
    hit the AIS table exactly. Marking those 'via callsign' would launder a guess."""
    monkeypatch.setattr(ais, "_callsign_cache", {"VRSQ4": {"name": "COSCO SHIPPING STAR", "mmsi": "1"}})
    monkeypatch.setattr(ais, "_vessel_cache", {})
    cands = proxy._resolver_candidates(
        [_chunk(10, "Gungor Star one three one five, correct.", callsign="VRSQ4")])
    assert cands == []


def test_resolver_input_lists_transmissions_and_candidates():
    text = proxy._render_resolver_input(
        [_chunk(30, "Maas Approach, Serenada.", cid=1)],
        [{"name": "SERENADA", "mmsi": "275545000", "via_callsign": True}])
    assert "1. [" in text and "Maas Approach, Serenada." in text
    assert "SERENADA" in text and "via callsign" in text


def test_resolver_input_says_so_when_there_are_no_candidates():
    text = proxy._render_resolver_input([_chunk(30, "hello", cid=1)], [])
    assert "none" in text.lower()


# Partial-callsign corroboration
#
# MSC TEMA VIII spelled 5LRK9 out as "five Lima Romeo Kilo nine"; Whisper heard "five DEMA
# Romeo, clear nine". The exact lookup could not fire, so the resolver was handed an empty
# candidate list and "unidentified" was the only answer available to it.

_TEMA = {"name": "MSC TEMA VIII", "mmsi": "636024193", "callsign": "5LRK9", "type": 91}
_REAL_CALL = ("Good afternoon, this is Motortanker MSC DEMA eight, "
              "Callsign five DEMA Romeo, clear nine.")


@pytest.fixture
def partial_caches(monkeypatch):
    monkeypatch.setattr(ais, "_callsign_cache", {"5LRK9": _TEMA})
    monkeypatch.setattr(ais, "_vessel_cache", {"MSC TEMA VIII": _TEMA})
    return _TEMA


def test_partial_callsign_becomes_a_candidate_when_the_name_agrees(partial_caches):
    """The reported miss: MSC TEMA VIII was in the cache and offered as no candidate."""
    cands = proxy._resolver_candidates([_chunk(30, _REAL_CALL, cid=1)])
    assert [c["name"] for c in cands] == ["MSC TEMA VIII"]
    assert cands[0]["via_partial_callsign"] is True
    assert cands[0]["partial_pattern"] == "5.R.9"


def test_partial_callsign_is_refused_when_no_name_corroborates(partial_caches):
    """The pattern alone is a guess. Without a name that agrees, offer nothing."""
    text = "Maas Approach, this is Wilson Durness, Callsign five DEMA Romeo, clear nine."
    assert proxy._resolver_candidates([_chunk(30, text, cid=1)]) == []


def test_partial_callsign_does_not_override_an_exact_match(monkeypatch, partial_caches):
    """An exact callsign is stronger evidence and must keep its mark.

    Both spellings must reach the same MMSI for this to test anything: one turn spells the
    callsign cleanly, a later one garbles it, and the vessel must stay marked exact.
    """
    monkeypatch.setattr(ais, "_callsign_cache", {"5LRK9": _TEMA, "PABC": _TEMA})
    cands = proxy._resolver_candidates([
        _chunk(40, "callsign papa alpha bravo charlie", cid=1, callsign="PABC"),
        _chunk(30, _REAL_CALL, cid=2),
    ])
    assert len(cands) == 1
    assert cands[0]["via_callsign"] is True
    assert "via_partial_callsign" not in cands[0]


def test_partial_callsign_can_be_disabled(monkeypatch, partial_caches):
    monkeypatch.setattr(conversations, "AIS_PARTIAL_CALLSIGN", False)
    assert proxy._resolver_candidates([_chunk(30, _REAL_CALL, cid=1)]) == []


# The live pass already matched a vessel against the whole AIS cache using the complete
# extracted name. _resolver_candidates never looked at it, and rebuilt its list from
# unigram/bigram probes instead -- strictly less information. Measured over 24 stored
# conversations that had a live match, that vessel was missing from the candidate list in 9
# of them: 7 resolved to nobody, 2 resolved to a different ship.

_SANTA = {"name": "SANTA ISABEL MAERSK", "mmsi": "219077000", "callsign": "OXWU2", "type": 71}
_ISABEL = {"name": "ISABEL", "mmsi": "244700279", "callsign": "PB7708", "type": 79}


@pytest.fixture
def santa_caches(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {"SANTA ISABEL MAERSK": _SANTA,
                                               "ISABEL": _ISABEL})
    monkeypatch.setattr(ais, "_callsign_cache", {"OXWU2": _SANTA, "PB7708": _ISABEL})


def test_the_live_match_becomes_a_candidate(santa_caches):
    """The reported miss: 'Santa Isabel Maas' hinted ISABEL at 100 (an exact substring word)
    while the real ship reached only 77.4 as a bigram, under a cutoff of 85."""
    chunks = [_chunk(30, "this is Santa Isabel Maas, Santa Isabel Maas.", cid=1,
                     vessel="SANTA ISABEL MAERSK", live_mmsi="219077000")]
    cands = proxy._resolver_candidates(chunks)
    by_mmsi = {c["mmsi"]: c for c in cands}
    assert "219077000" in by_mmsi, "the vessel the live pass matched must be offered"
    assert by_mmsi["219077000"]["via_live_match"] is True


def test_live_candidate_does_not_displace_an_exact_callsign(santa_caches):
    """An exact callsign is stronger; the live guess must not overwrite its mark."""
    chunks = [_chunk(30, "callsign oscar xray whiskey uniform two", cid=1, callsign="OXWU2",
                     vessel="ISABEL", live_mmsi="244700279")]
    cands = proxy._resolver_candidates(chunks)
    exact = [c for c in cands if c["mmsi"] == "219077000"]
    assert exact and exact[0]["via_callsign"] is True
    assert "via_live_match" not in exact[0]


def test_live_candidate_is_skipped_without_an_mmsi(santa_caches):
    """A live guess that never matched AIS is a bare string -- nothing to offer."""
    chunks = [_chunk(30, "Maas Approach, over.", cid=1, vessel="SOMETHING", live_mmsi=None)]
    assert [c for c in proxy._resolver_candidates(chunks) if c.get("via_live_match")] == []


def test_live_candidate_is_skipped_when_the_mmsi_left_the_cache(santa_caches):
    chunks = [_chunk(30, "Maas Approach, over.", cid=1, vessel="GONE", live_mmsi="999999999")]
    assert [c for c in proxy._resolver_candidates(chunks) if c.get("via_live_match")] == []


def test_live_candidates_can_be_disabled(monkeypatch, santa_caches):
    monkeypatch.setattr(conversations, "RESOLVER_LIVE_CANDIDATES", False)
    chunks = [_chunk(30, "this is Santa Isabel Maas.", cid=1,
                     vessel="SANTA ISABEL MAERSK", live_mmsi="219077000")]
    assert [c for c in proxy._resolver_candidates(chunks) if c.get("via_live_match")] == []


def test_live_candidate_is_marked_in_the_resolver_prompt():
    text = proxy._render_resolver_input(
        [_chunk(30, "this is Santa Isabel Maas.", cid=1)],
        [dict(_SANTA, via_live_match=True)])
    assert "live pass matched this name" in text


def test_the_reported_conversation_now_yields_a_candidate(monkeypatch):
    """12:09 on 2026-07-31: MSC TEMA VIII spelled its callsign out and the resolver was
    handed an empty candidate list, so 'unidentified' was the only answer available."""
    monkeypatch.setattr(ais, "_callsign_cache", {"5LRK9": _TEMA})
    monkeypatch.setattr(ais, "_vessel_cache", {"MSC TEMA VIII": _TEMA})
    window = [
        _chunk(60, "Maas Approach, Maas Approach, MST, FEMA 8, good afternoon sir.", cid=1),
        _chunk(50, _REAL_CALL, cid=2),
        _chunk(40, "Maas Approach, bring your message.", cid=3),
    ]
    rendered = proxy._render_resolver_input(window, proxy._resolver_candidates(window))
    assert "MSC TEMA VIII (MMSI:636024193)" in rendered
    assert "partial callsign 5.R.9" in rendered
    assert "(none" not in rendered


def test_partial_candidate_is_marked_in_the_resolver_prompt():
    text = proxy._render_resolver_input(
        [_chunk(30, _REAL_CALL, cid=1)],
        [{"name": "MSC TEMA VIII", "mmsi": "636024193", "callsign": "5LRK9",
          "via_partial_callsign": True, "partial_pattern": "5.R.9"}])
    assert "partial callsign 5.R.9, name corroborated" in text


# ---------------------------------------------------------------------------
# Exact callsign outranks name similarity
#
# 10:03 on 2026-08-04: PECHORA STAR spelled 9HA2788 out cleanly and was in the AIS cache the
# whole time, yet the exchange resolved to nobody. enrich_with_ais tried match_by_name first,
# and the word-window fallback probed "IKORA" alone into VIKTORIA at 76.9 -- one point over
# the cutoff. Because that returned something, the exact callsign lookup never ran; then the
# matched vessel's callsign overwrote the spoken one, so the journal recorded DB6442.
# _resolver_candidates then correctly refused DB6442 (not readable out of the transmission)
# and the retrospective pass -- which exists to recover exactly this -- was never offered the
# ship at all.
#
# Two of the three lost callsigns in the 300-conversation store failed this way; ECO ROYALTY
# (V7LA9) lost the same way to ELKA, on a turn whose neighbours resolved correctly.
# ---------------------------------------------------------------------------

_PECHORA = {"name": "PECHORA STAR", "mmsi": "215760000", "callsign": "9HA2788", "type": 89}
_VIKTORIA = {"name": "VIKTORIA", "mmsi": "211522860", "callsign": "DB6442", "type": 80}

_PECHORA_CALL = ("Maaas Approach, Maaas Approach, this is Motortanker, Ikora Star, "
                 "callsign nine, Hotel Alpha, two seven eight eight, calling on channel "
                 "zero one, over. Ikora Star, this is Motortanker.")


@pytest.fixture
def pechora_caches(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {"PECHORA STAR": _PECHORA, "VIKTORIA": _VIKTORIA})
    monkeypatch.setattr(ais, "_callsign_cache", {"9HA2788": _PECHORA, "DB6442": _VIKTORIA})


def test_a_spoken_callsign_outranks_a_fuzzy_name_match(pechora_caches):
    """The reported miss. An exact hit on a callsign that was verifiably spelled out is the
    strongest evidence here; a 76-point name ratio is the weakest."""
    result = proxy.enrich_with_ais(
        {"vessel": "Ikora Star", "callsign": "9HA2788"}, _PECHORA_CALL)
    assert result["vessel"] == "PECHORA STAR"
    assert result["mmsi"] == "215760000"
    assert result["match_method"] == "callsign"


def test_the_spoken_callsign_survives_enrichment(pechora_caches):
    """What blinded the resolver: the matched vessel's callsign replaced the spoken one, so
    the journal no longer held anything readable out of the transmission."""
    result = proxy.enrich_with_ais(
        {"vessel": "Ikora Star", "callsign": "9HA2788"}, _PECHORA_CALL)
    assert result["callsign"] == "9HA2788"


def test_enrichment_still_supplies_a_callsign_nobody_spoke(pechora_caches):
    """Enrichment of a name-only match is unchanged -- that is where AIS adds detail."""
    result = proxy.enrich_with_ais({"vessel": "Viktoria", "callsign": None}, "this is Viktoria")
    assert result["vessel"] == "VIKTORIA"
    assert result["callsign"] == "DB6442"
    assert result["match_method"] == "name"


def test_an_unspoken_callsign_never_wins_the_lookup(pechora_caches):
    """A callsign that cannot be read out of the transmission is a guess that happens to hit
    the table. Promoting it above the name would launder that guess into an identity."""
    result = proxy.enrich_with_ais(
        {"vessel": "Viktoria", "callsign": "9HA2788"}, "Maas Approach, this is Viktoria, over.")
    assert result["vessel"] == "VIKTORIA"
    assert result["match_method"] == "name"


def test_enrichment_falls_back_to_the_name_when_the_callsign_is_unknown(pechora_caches):
    """Spelled out, verified, but not in the cache: the name is all that is left."""
    result = proxy.enrich_with_ais(
        {"vessel": "Viktoria", "callsign": "PABC"},
        "this is Viktoria, callsign papa alpha bravo charlie, over.")
    assert result["vessel"] == "VIKTORIA"
    assert result["match_method"] == "name"


def test_enrichment_without_the_transmission_keeps_the_old_order(pechora_caches):
    """No text means no way to verify the callsign was spoken, so it stays a fallback."""
    result = proxy.enrich_with_ais({"vessel": "Ikora Star", "callsign": "9HA2788"})
    assert result["vessel"] == "VIKTORIA"


def test_enrichment_returns_the_result_untouched_when_nothing_matches(pechora_caches):
    result = proxy.enrich_with_ais({"vessel": "Nobody At All", "callsign": None}, "hello")
    assert result == {"vessel": "Nobody At All", "callsign": None}


# The resolver read the journalled callsign, so a live pass that recorded the wrong one (or
# none) took the exact lookup down with it. The transmission text is the primary source and
# is already stored verbatim -- decode from that instead, and the retrospective pass stops
# depending on the live guess it exists to second-guess.

def test_the_resolver_decodes_the_callsign_from_the_transmission(pechora_caches):
    """The journal holds VIKTORIA's callsign, exactly as the reported conversation recorded
    it. The spelled-out 9HA2788 is still right there in the text."""
    chunks = [_chunk(30, _PECHORA_CALL, cid=1, callsign="DB6442",
                     vessel="VIKTORIA", live_mmsi="211522860")]
    exact = [c for c in proxy._resolver_candidates(chunks) if c.get("via_callsign")]
    assert [c["name"] for c in exact] == ["PECHORA STAR"]


def test_the_resolver_finds_a_callsign_the_live_pass_never_extracted(monkeypatch):
    """MONA SWAN, the third lost callsign: the shore station asked for it and the vessel
    spelled it out, but no callsign reached the journal at all."""
    mona = {"name": "MONA SWAN", "mmsi": "219624000", "callsign": "OWGJ2"}
    monkeypatch.setattr(ais, "_callsign_cache", {"OWGJ2": mona})
    monkeypatch.setattr(ais, "_vessel_cache", {})
    chunks = [_chunk(30, "Monaas one, good afternoon, this is Maas Approach, confirm your "
                         "callsign, Oscar Whiskey, Gulf Juliet two.", cid=1, callsign=None)]
    exact = [c for c in proxy._resolver_candidates(chunks) if c.get("via_callsign")]
    assert [c["name"] for c in exact] == ["MONA SWAN"]


def test_the_resolver_still_refuses_a_callsign_nobody_spoke(monkeypatch):
    """Unchanged: decoding from the text must not weaken the guard that keeps an invented
    callsign from being marked as evidence."""
    monkeypatch.setattr(ais, "_callsign_cache", {"VRSQ4": {"name": "COSCO SHIPPING STAR", "mmsi": "1"}})
    monkeypatch.setattr(ais, "_vessel_cache", {})
    assert proxy._resolver_candidates(
        [_chunk(10, "Gungor Star one three one five, correct.", callsign="VRSQ4")]) == []


def test_the_reported_pechora_conversation_now_yields_the_candidate(pechora_caches):
    """10:03 on 2026-08-04, end to end: the window that resolved to nobody."""
    window = [
        _chunk(60, _PECHORA_CALL, cid=1, callsign="DB6442", vessel="VIKTORIA",
               live_mmsi="211522860"),
        _chunk(50, "Can you please confirm our pilot boarding time?", cid=2),
        _chunk(40, "Bekoa Star, yes, correct, pilot line up, both sides, two meters.", cid=3),
    ]
    rendered = proxy._render_resolver_input(window, proxy._resolver_candidates(window))
    assert "PECHORA STAR (MMSI:215760000)" in rendered
    assert "via callsign, exact match" in rendered


# resolve_conversation itself was never exercised -- only the helpers either side of it --
# so `re`, used solely on the fenced-reply branch, went missing in the module split and
# nothing failed. Haiku fences its JSON every time, so in production that branch was the
# only branch: every conversation raised NameError, was swallowed by the broad `except
# Exception`, and surfaced as the ordinary-looking "resolver unavailable".
class _StubClaude:
    def __init__(self, reply):
        self._reply = reply
        self.messages = self
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        text = self._reply
        return type("R", (), {"content": [type("C", (), {"text": text})()]})()


@pytest.fixture
def stub_claude(monkeypatch):
    def _install(reply):
        stub = _StubClaude(reply)
        monkeypatch.setattr(conversations, "_get_claude", lambda: stub)
        return stub
    return _install


def test_the_resolver_samples_deterministically(stub_claude, monkeypatch):
    """Left at the API default of 1.0 until 2026-08-08, while identify.py pinned 0 all along.

    Repeatability matters more here than a point of accuracy: without it, every
    `bench_identify.py --resolve` A/B measures the change *plus* the sampling noise with no
    way to separate them. Two runs that day named different off-list vessels from identical
    inputs. The queued adjudicator-precedence work is unmeasurable until this holds.
    """
    stub = stub_claude(_EXCHANGE)
    monkeypatch.setattr(ais, "_vessel_cache", {"SERENADA": {"name": "SERENADA",
                                                            "mmsi": "275545000"}})
    proxy.resolve_conversation([_chunk(30, "Maas Approach, Serenada.", cid=1)])
    assert stub.kwargs["temperature"] == 0


_EXCHANGE = ('{"exchanges": [{"chunk_ids": [1, 2], "vessel": "SERENADA", '
             '"mmsi": "275545000", "evidence": "callsign", "confidence": "high"}]}')


@pytest.mark.parametrize("reply", [
    _EXCHANGE,                                   # bare JSON
    f"```json\n{_EXCHANGE}\n```",                # what Haiku actually returns
    f"```\n{_EXCHANGE}\n```",
    f"Here you go:\n```json\n{_EXCHANGE}\n```\n",
])
def test_resolve_conversation_reads_the_reply_however_it_is_wrapped(reply, stub_claude,
                                                                    monkeypatch):
    stub_claude(reply)
    monkeypatch.setattr(ais, "_vessel_cache", {"SERENADA": {"name": "SERENADA",
                                                            "mmsi": "275545000"}})
    chunks = [_chunk(30, "Maas Approach, Serenada.", cid=1), _chunk(20, "Roger.", cid=2)]
    result = proxy.resolve_conversation(chunks)
    assert [e["vessel"] for e in result] == ["SERENADA"]
    assert result[0]["evidence"] != "resolver unavailable"


def test_resolve_conversation_still_degrades_when_the_reply_is_unusable(stub_claude):
    """The fallback must stay reachable -- it just must not be the only outcome."""
    stub_claude("I'm afraid I can't help with that.")
    result = proxy.resolve_conversation([_chunk(30, "Maas Approach.", cid=1)])
    assert result[0]["evidence"] == "resolver unavailable"


def test_fuzzy_buffer_match_finds_a_restated_name(buffer):
    """Also lost its import in the split, on a branch no test reached."""
    buffer.append({"time": datetime.datetime.now(), "vessel": "SERENADA",
                   "fuzzy": True, "result": {}})
    entry, index = proxy._find_fuzzy_match_in_buffer("Selenada")
    assert index == 0 and entry["vessel"] == "SERENADA"


# ---------------------------------------------------------------------------
# Stored conversations and the page
# ---------------------------------------------------------------------------

def test_stored_turn_text_is_copied_verbatim_from_the_journal(monkeypatch):
    """The whole point of resolving afterwards: transcriptions must be untouched."""
    saved = []
    monkeypatch.setattr(conversations, "_resolved", saved)
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    window = [_chunk(30, "Maas Approach, Selenada.", cid=1), _chunk(20, "Roger, over.", cid=2)]
    original = [c["text"] for c in window]

    proxy._store_resolved(window, [{"chunk_ids": [1, 2], "vessel": "SERENADA", "mmsi": "275545000",
                                    "evidence": "later turn", "confidence": "high"}])

    assert [t["text"] for t in saved[0]["turns"]] == original
    assert [c["text"] for c in window] == original, "resolution must not mutate the journal"


# Vessel particulars on /conversations
#
# The AIS match already carries dimensions, IMO and a position, and /identified-vessels
# shows them -- but /conversations, which is where identity is actually settled, showed only
# the name and MMSI. The particulars are snapshotted when the exchange resolves rather than
# looked up when the page renders: position, speed and course are live values, so rendering
# an hours-old exchange against the ship's current position would claim it was somewhere it
# was not when it called.

_RICH = {"name": "PECHORA STAR", "mmsi": "215760000", "callsign": "9HA2788", "type": 89,
         "imo": "9123456", "length": 129, "beam": 21, "latitude": 51.92, "longitude": 3.5378,
         "sog": 8.2, "cog": 43.0, "heading": 45}


def test_validate_snapshots_the_vessel_particulars():
    out = proxy._validate_exchanges(
        [{"chunk_ids": [1], "vessel": "PECHORA STAR", "confidence": "high"}],
        [_chunk(30, cid=1)], {"PECHORA STAR": _RICH})
    assert out[0]["imo"] == "9123456"
    assert (out[0]["length"], out[0]["beam"]) == (129, 21)
    assert (out[0]["latitude"], out[0]["longitude"]) == (51.92, 3.5378)
    assert (out[0]["sog"], out[0]["cog"]) == (8.2, 43.0)


def test_validate_leaves_particulars_empty_when_nobody_was_identified():
    out = proxy._validate_exchanges(
        [{"chunk_ids": [1], "vessel": None}], [_chunk(30, cid=1)], {})
    assert out[0]["imo"] is None and out[0]["length"] is None
    assert out[0]["latitude"] is None and out[0]["sog"] is None


def test_page_shows_draught_and_destination():
    html = proxy.render_conversations_page([{
        "vessel": "ANOUK", "mmsi": "1", "confidence": "high",
        "length": 110, "beam": 11, "draught": 3.3, "destination": "ROTTERDAM",
        "start": "2026-08-04 12:17:01", "end": "2026-08-04 12:17:51", "turns": [],
    }])
    assert "draught 3.3 m" in html
    assert "ROTTERDAM" in html


def test_a_hostile_destination_cannot_break_the_page():
    """Destination is free text off the radio -- the most attacker-controllable field on
    the feed, since anyone with a transmitter can set it to whatever they like."""
    html = proxy.render_conversations_page([{
        "vessel": "EVIL", "mmsi": "1", "confidence": "high",
        "destination": "<img src=x onerror=alert(1)>",
        "start": "2026-08-04 12:00:00", "end": "2026-08-04 12:00:30", "turns": [],
    }])
    assert "<img" not in html
    assert "&lt;img" in html


def test_page_shows_the_vessel_particulars():
    html = proxy.render_conversations_page([{
        "vessel": "PECHORA STAR", "mmsi": "215760000", "confidence": "high",
        "imo": "9123456", "length": 129, "beam": 21,
        "latitude": 51.92, "longitude": 3.5378, "sog": 8.2, "cog": 43.0,
        "start": "2026-08-04 10:03:36", "end": "2026-08-04 10:04:04", "turns": [],
    }])
    assert "IMO 9123456" in html
    assert "129 &times; 21 m" in html
    assert "8.2 kn" in html
    assert "43&deg;" in html
    assert "51.9200, 3.5378" in html


def test_page_omits_particulars_that_are_missing():
    """A vessel matched by callsign alone carries no position: show the dimensions it has
    and say nothing about where it is, rather than rendering a row of dashes."""
    html = proxy.render_conversations_page([{
        "vessel": "PECHORA STAR", "mmsi": "215760000", "confidence": "high",
        "length": 129, "beam": 21,
        "start": "2026-08-04 10:03:36", "end": "2026-08-04 10:04:04", "turns": [],
    }])
    assert "129 &times; 21 m" in html
    assert "kn" not in html and "IMO" not in html


def test_page_still_renders_a_row_stored_before_particulars_existed():
    """The 104 conversations already on disk have none of these keys."""
    html = proxy.render_conversations_page([{
        "vessel": "VISTA", "mmsi": "538009952", "confidence": "high",
        "start": "2026-07-31 12:00:00", "end": "2026-07-31 12:00:30", "turns": [],
    }])
    assert "VISTA" in html
    assert 'class="ais"' not in html


def test_page_cannot_be_broken_out_of_by_a_hostile_imo():
    """IMO comes off the AIS feed like everything else here -- broadcast in the clear, so
    attacker-controllable by anyone with a transmitter in the Rotterdam box."""
    html = proxy.render_conversations_page([{
        "vessel": "EVIL", "mmsi": "1", "confidence": "high", "imo": "<script>alert(1)</script>",
        "start": "2026-07-31 12:00:00", "end": "2026-07-31 12:00:30", "turns": [],
    }])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_page_renders_with_no_conversations():
    assert "No conversations resolved yet" in proxy.render_conversations_page([])


def test_page_links_an_identified_vessel_to_vesselfinder():
    """The MMSI, not the name: names are neither unique nor reliably heard, and the MMSI is
    what the AIS match actually established."""
    html = proxy.render_conversations_page([{
        "vessel": "VISTA", "mmsi": "538009952", "confidence": "high",
        "start": "2026-07-31 12:00:00", "end": "2026-07-31 12:00:30", "turns": [],
    }])
    assert ('<a class="vf" href="https://www.vesselfinder.com/vessels?name=538009952" '
            'target="_blank" rel="noopener noreferrer">VISTA</a>') in html


def test_page_does_not_link_a_vessel_with_no_mmsi():
    """A name without an MMSI has nothing to look up -- render it plain, not as a dead link."""
    html = proxy.render_conversations_page([{
        "vessel": "MSC TEMA VIII", "mmsi": None, "confidence": "medium",
        "start": "2026-07-31 12:00:00", "end": "2026-07-31 12:00:30", "turns": [],
    }])
    assert "MSC TEMA VIII" in html
    assert "vesselfinder.com" not in html


def test_page_does_not_link_an_unidentified_conversation():
    html = proxy.render_conversations_page([{
        "vessel": None, "mmsi": None, "confidence": "low",
        "start": "2026-07-31 12:00:00", "end": "2026-07-31 12:00:30", "turns": [],
    }])
    assert "unidentified" in html
    assert "vesselfinder.com" not in html


def test_page_cannot_be_broken_out_of_by_a_hostile_mmsi():
    """The MMSI comes off an external AIS feed, so it is never trusted into an href raw."""
    html = proxy.render_conversations_page([{
        "vessel": "EVIL", "mmsi": '" onmouseover="alert(1)', "confidence": "high",
        "start": "2026-07-31 12:00:00", "end": "2026-07-31 12:00:30", "turns": [],
    }])
    # The href must be exactly the encoded URL, so nothing escaped the attribute. The same
    # value also appears further down as escaped text content, which is separately safe --
    # hence asserting on the anchor specifically rather than on the whole page.
    assert ('<a class="vf" href="https://www.vesselfinder.com/vessels'
            '?name=%22%20onmouseover%3D%22alert%281%29" '
            'target="_blank" rel="noopener noreferrer">EVIL</a>') in html


# ---------------------------------------------------------------------------
# The per-transmission vessels log
#
# This module had no tests. It also interpolated every field into HTML unescaped, which
# matters more than it looks: a vessel name arrives from the aisstream feed, and anyone
# with a transmitter can broadcast AIS static data carrying whatever name they like.
# ---------------------------------------------------------------------------

@pytest.fixture
def vessels_log(tmp_path, monkeypatch):
    path = tmp_path / "identified_vessels.html"
    monkeypatch.setattr(vessel_log, "VESSELS_LOG_FILE", str(path))
    proxy._init_vessels_log()
    return path


def test_vessels_log_links_an_identified_vessel(vessels_log):
    proxy._append_vessel_to_log(
        {"vessel": "VISTA", "mmsi": "538009952", "callsign": "V7A5384"}, "Maas Approach, Vista.")
    assert ('<a class="vf" href="https://www.vesselfinder.com/vessels?name=538009952" '
            'target="_blank" rel="noopener noreferrer">VISTA</a>') in vessels_log.read_text(
                encoding="utf-8")


def test_vessels_log_leaves_an_unmatched_row_unlinked(vessels_log):
    proxy._append_vessel_to_log({"vessel": None, "mmsi": None}, "Maas Approach.")
    html = vessels_log.read_text(encoding="utf-8")
    assert "vesselfinder.com" not in html
    assert 'class="no-match"' in html


def test_vessels_log_escapes_a_hostile_ais_vessel_name(vessels_log):
    """AIS static data is broadcast by anyone; the name reaches this page verbatim."""
    proxy._append_vessel_to_log(
        {"vessel": "<script>alert(1)</script>", "mmsi": "1", "callsign": "<img>"}, "hi")
    html = vessels_log.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img>" not in html


def test_vessels_log_escapes_the_transcription(vessels_log):
    proxy._append_vessel_to_log({"vessel": "X", "mmsi": "1"}, "<b>not bold</b> & co")
    html = vessels_log.read_text(encoding="utf-8")
    assert "<b>not bold</b>" not in html
    assert "&lt;b&gt;not bold&lt;/b&gt; &amp; co" in html


def test_page_shows_the_resolved_identity_and_a_disagreeing_live_guess():
    html = proxy.render_conversations_page([{
        "vessel": "SERENADA", "mmsi": "275545000", "confidence": "high", "via_callsign": True,
        "evidence": "callsign PABC", "channel": "160,650",
        "start": "2026-07-30 11:31:27", "end": "2026-07-30 11:31:57",
        "turns": [{"time": "11:31:27", "text": "Maas Approach, Selenada.", "live_vessel": "AD"}],
    }])
    assert "SERENADA" in html and "via callsign" in html
    assert "live: AD" in html, "a corrected live guess should stay visible"
    assert "Maas Approach, Selenada." in html


def test_page_escapes_html_in_transcriptions():
    html = proxy.render_conversations_page([{
        "vessel": None, "confidence": "low", "evidence": "", "channel": "160,650",
        "start": "s", "end": "e",
        "turns": [{"time": "11:00:00", "text": "<script>alert(1)</script>", "live_vessel": None}],
    }])
    assert "<script>" not in html and "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Mode scoping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("what is your cosine", "Callsign"),
    ("what is your call sign", "Callsign"),
])
def test_shared_corrections_apply_to_both_bands(raw, expected):
    assert expected in proxy._apply_sttt_corrections(raw, mode="maritime")
    assert expected in proxy._apply_sttt_corrections(raw, mode="airband")


@pytest.mark.parametrize("raw,maritime_only", [
    ("draft twelve metres", "draught"),
    ("watch out for the boys", "buoys"),
    ("motor tanker Neptune", "Motortanker"),
    ("mass approach, over", "Maas"),
])
def test_maritime_corrections_do_not_fire_on_airband(raw, maritime_only):
    assert maritime_only in proxy._apply_sttt_corrections(raw, mode="maritime")
    assert maritime_only not in proxy._apply_sttt_corrections(raw, mode="airband")


def test_airband_keeps_aviation_phraseology_intact():
    """'final approach' and 'draft' are ordinary aviation words -- rewriting them to
    'Maas Approach' and 'draught' would corrupt real airband traffic."""
    text = "cleared for final approach, check the draft of the flight plan"
    assert proxy._apply_sttt_corrections(text, mode="airband") == text


def test_corrections_default_to_maritime():
    assert "Maas" in proxy._apply_sttt_corrections("mass approach, over")


# ---------------------------------------------------------------------------
# Fuzzy "<x> Approach" -> "Maas Approach"
#
# Groq spells Maas 13 different ways; fixed regexes derived from one sample did not
# generalise (0.3 WER points held out, vs 3.7 for this).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", [
    "Aas", "AAS", "MAAAS", "Mass", "Mars", "Mase", "Mast", "maas",
])
def test_fuzzy_maas_normalises_known_variants(variant):
    result = proxy._correct_maas_before_approach(f"{variant} approach, this is Neptune")
    assert result.startswith("Maas Approach")


@pytest.mark.parametrize("spelling", [
    "approach", "Approach", "Aproach",
    # A recognised approach-word is a precondition for the rule firing at all, so a spelling
    # the pattern missed took the Maas correction down with it: "Aas Aapproach" was left
    # entirely alone despite "Aas" scoring 85.7. 7 clips carried "Aapproach", 1 "Proach".
    "Aapproach", "Proach",
])
def test_fuzzy_maas_tolerates_misspelled_approach(spelling):
    assert proxy._correct_maas_before_approach(f"Aas {spelling}").startswith("Maas Approach")


# Threshold 70 recognised only half the variants the references show as "Maas Approach".
# Measured over the 636 benchmarked transmissions, dropping it to 50 corrects 54 more rows
# across 27 clips and damages none; split-half validated at -1.04 / -1.51 WER points.
@pytest.mark.parametrize("variant", [
    "Aps", "Master", "Marsh", "MOTS", "Must", "Last", "Mous", "Airmass", "Kalmars", "Amass",
])
def test_fuzzy_maas_reaches_the_variants_the_references_show(variant):
    result = proxy._correct_maas_before_approach(f"{variant} approach, this is Neptune")
    assert result.startswith("Maas Approach"), f"{variant} is 'Maas Approach' in the references"


@pytest.mark.parametrize("text", [
    "Rotterdam Approach, this is Neptune",
    "cleared for final approach",
    "Schiphol approach, good morning",
])
def test_fuzzy_maas_leaves_dissimilar_names_alone(text):
    assert proxy._correct_maas_before_approach(text) == text


@pytest.mark.parametrize("text", [
    "we are approaching the buoy",
    "I'm approaching Maasvlakte",
])
def test_fuzzy_maas_leaves_the_verb_alone(text):
    """Every "approaching" in the references is ordinary English ("are approaching",
    "I'm approaching"); only the noun is ever the station. The old rule also silently ate
    the "ing", rewriting "mass approaching" as "Maas Approach"."""
    assert proxy._correct_maas_before_approach(text) == text


def test_fuzzy_maas_does_not_swallow_a_vessel_name():
    """Clip 0037 is "Starfighter, Maas Approach" with the comma lost in decoding. Replacing
    whatever precedes "approach" scores better overall but deletes the ship, which is the one
    thing the identification path cannot afford -- so the rule stays similarity-gated."""
    assert "Starfighter" in proxy._correct_maas_before_approach("Starfighter Aapproach")


def test_fuzzy_maas_is_applied_through_the_maritime_pipeline():
    assert "Maas Approach" in proxy._apply_sttt_corrections("AAS approach, AAS approach, Fjordstrom")


def test_fuzzy_maas_is_not_applied_on_airband():
    text = "Aas approach"
    assert proxy._apply_sttt_corrections(text, mode="airband") == text


# Maas Center, and "anchor"
#
# Two smaller rules from the same substitution sweep. Both are clean but thin -- 2 clips
# each, against 4 for the ladder rule -- so they are worth roughly a tenth of a WER point
# apiece rather than the 1.24 the approach widening is worth.

@pytest.mark.parametrize("variant", ["Maaf", "Mass", "Mast", "Aas"])
def test_fuzzy_maas_normalises_the_center_too(variant):
    """"Maas Center, Recon buoy" is read out as often as the approach call, and the same
    mis-spellings land on it -- "Maaf Center, Rekkenbooi"."""
    assert proxy._apply_sttt_corrections(f"{variant} Center, Recon buoy").startswith("Maas Center")


@pytest.mark.parametrize("text", [
    "Rotterdam Center, over",
    "the traffic center is closed",
])
def test_center_rule_leaves_dissimilar_names_alone(text):
    assert proxy._apply_sttt_corrections(text) == text


def test_center_rule_is_not_applied_on_airband():
    text = "Aas Center"
    assert proxy._apply_sttt_corrections(text, mode="airband") == text


@pytest.mark.parametrize("raw,expected", [
    ("we are at Angkor, India", "anchor"),
    ("heave up Angkor", "heave up anchor"),
])
def test_anchor_is_corrected(raw, expected):
    assert expected in proxy._apply_sttt_corrections(raw)


def test_anchor_rule_is_not_applied_on_airband():
    text = "we are at Angkor"
    assert proxy._apply_sttt_corrections(text, mode="airband") == text


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
    monkeypatch.setattr(backends.http.client, "HTTPSConnection", _FakeConnection)
    monkeypatch.setattr(backends, "GROQ_API_KEY", "gsk_test_key")
    return _FakeConnection


_FILE_INFO = {"field": "file", "filename": "audio.wav", "content_type": "audio/wav", "data": b"RIFFfake"}


def test_transcribe_dispatches_to_groq_when_selected(monkeypatch):
    monkeypatch.setattr(backends, "STT_BACKEND", "groq")
    monkeypatch.setattr(backends, "_transcribe_groq", lambda *a, **k: (200, b'{"text":"groq"}', []))
    monkeypatch.setattr(backends, "_transcribe_whisper_cpp", lambda *a, **k: pytest.fail("wrong backend"))

    status, body, _ = proxy.transcribe(_FILE_INFO, language="en", prompt="")
    assert (status, body) == (200, b'{"text":"groq"}')


def test_transcribe_dispatches_to_whisper_cpp_when_selected(monkeypatch):
    monkeypatch.setattr(backends, "STT_BACKEND", "whisper_cpp")
    monkeypatch.setattr(backends, "_transcribe_whisper_cpp", lambda *a, **k: (200, b'{"text":"local"}', []))
    monkeypatch.setattr(backends, "_transcribe_groq", lambda *a, **k: pytest.fail("wrong backend"))

    status, body, _ = proxy.transcribe(_FILE_INFO, language="en", prompt="")
    assert (status, body) == (200, b'{"text":"local"}')


def test_transcribe_groq_missing_key_returns_error_envelope(monkeypatch):
    monkeypatch.setattr(backends, "GROQ_API_KEY", "")
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

    monkeypatch.setattr(backends.http.client, "HTTPSConnection", boom)

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
    monkeypatch.setattr(backends.time, "sleep", slept.append)
    fake_groq.responses = [
        _FakeResponse(429, b'{"error":"rate limited"}', {"Retry-After": "1.5"}),
        _FakeResponse(200, b'{"text":"after wait"}'),
    ]

    status, body, _ = proxy._transcribe_groq(_FILE_INFO, language="en", prompt="")

    assert status == 200
    assert json.loads(body)["text"] == "after wait"
    assert slept == [1.5]


# ---------------------------------------------------------------------------
# Response header filtering
#
# WhisperClient.ReadToEndAsync reads until EOF and ignores Content-Length, so the socket
# MUST close for the plugin to ever see a response. Groq sits behind Cloudflare and
# returns "Connection: keep-alive"; forwarding it verbatim makes
# BaseHTTPRequestHandler.send_header set close_connection = False, the socket stays open,
# and every chunk dies on the plugin's 60s timeout with the body already delivered.
# ---------------------------------------------------------------------------

_GROQ_REAL_HEADERS = [
    ("Date", "Thu, 30 Jul 2026 09:48:57 GMT"),
    ("Content-Type", "application/json"),
    ("Connection", "keep-alive"),
    ("Cache-Control", "private, max-age=0, no-store"),
    ("Server", "cloudflare"),
    ("vary", "Origin"),
    ("x-ratelimit-remaining-requests", "1935"),
    ("x-request-id", "req_01kys6tnaxftma7vh1w5w4s4t8"),
    ("set-cookie", "__cf_bm=WX_k4xin; HttpOnly; Secure; Domain=groq.com"),
    ("CF-RAY", "a233735b9921b927-AMS"),
    ("alt-svc", 'h3=":443"; ma=86400'),
    ("Content-Length", "90"),
]


def _names(headers):
    return {k.lower() for k, _ in headers}


@pytest.mark.parametrize("dropped", ["connection", "keep-alive", "transfer-encoding"])
def test_hop_by_hop_headers_are_never_forwarded(dropped):
    """RFC 7230 hop-by-hop headers describe one connection and must not be relayed."""
    src = _GROQ_REAL_HEADERS + [("Keep-Alive", "timeout=5"), ("Transfer-Encoding", "chunked")]
    assert dropped not in _names(proxy._client_response_headers(src))


def test_connection_keep_alive_is_stripped_from_a_real_groq_response():
    assert "connection" not in _names(proxy._client_response_headers(_GROQ_REAL_HEADERS))


@pytest.mark.parametrize("dropped", ["content-length", "content-encoding"])
def test_framing_headers_are_dropped_because_the_body_is_rewritten(dropped):
    src = _GROQ_REAL_HEADERS + [("Content-Encoding", "gzip")]
    assert dropped not in _names(proxy._client_response_headers(src))


@pytest.mark.parametrize("dropped", ["date", "server"])
def test_upstream_date_and_server_are_dropped(dropped):
    """send_response() emits its own; forwarding these produced duplicate headers."""
    assert dropped not in _names(proxy._client_response_headers(_GROQ_REAL_HEADERS))


@pytest.mark.parametrize("dropped", ["set-cookie", "alt-svc", "cf-ray", "cache-control", "vary"])
def test_cdn_noise_is_not_relayed_to_the_plugin(dropped):
    """A Cloudflare session cookie has no meaning to an SDR# plugin and should not leak."""
    assert dropped not in _names(proxy._client_response_headers(_GROQ_REAL_HEADERS))


@pytest.mark.parametrize("kept", ["content-type", "x-ratelimit-remaining-requests", "x-request-id"])
def test_useful_headers_survive(kept):
    assert kept in _names(proxy._client_response_headers(_GROQ_REAL_HEADERS))


def test_header_values_are_preserved():
    out = dict((k.lower(), v) for k, v in proxy._client_response_headers(_GROQ_REAL_HEADERS))
    assert out["content-type"] == "application/json"
    assert out["x-ratelimit-remaining-requests"] == "1935"


def test_empty_upstream_headers_are_handled():
    assert proxy._client_response_headers([]) == []


# ---------------------------------------------------------------------------
# Daily quota warnings
# ---------------------------------------------------------------------------

@pytest.fixture
def quota(monkeypatch, capsys):
    """Reset the module-level warning state so each test starts un-warned."""
    monkeypatch.setattr(backends, "_quota_last_bucket", None)
    capsys.readouterr()
    return capsys


def _quota_headers(remaining):
    return [("Content-Type", "application/json"), ("x-ratelimit-remaining-requests", str(remaining))]


def test_quota_silent_when_plenty_remains(quota):
    proxy._check_groq_quota(_quota_headers(1999))
    assert quota.readouterr().out == ""


def test_quota_warns_once_below_threshold(quota):
    proxy._check_groq_quota(_quota_headers(180))
    out = quota.readouterr().out
    assert "Groq daily requests remaining: 180" in out


def test_quota_does_not_repeat_within_the_same_bucket(quota):
    proxy._check_groq_quota(_quota_headers(180))
    quota.readouterr()
    for remaining in (179, 165, 151):
        proxy._check_groq_quota(_quota_headers(remaining))
    assert quota.readouterr().out == ""


def test_quota_warns_again_on_the_next_bucket(quota):
    proxy._check_groq_quota(_quota_headers(180))
    quota.readouterr()
    proxy._check_groq_quota(_quota_headers(149))
    assert "remaining: 149" in quota.readouterr().out


def test_quota_rearms_after_the_daily_reset(quota):
    """A quota rollover must not leave warnings suppressed for the next day."""
    proxy._check_groq_quota(_quota_headers(60))
    quota.readouterr()
    proxy._check_groq_quota(_quota_headers(2000))   # new day
    assert quota.readouterr().out == ""
    proxy._check_groq_quota(_quota_headers(180))
    assert "remaining: 180" in quota.readouterr().out


@pytest.mark.parametrize("headers", [
    [],
    [("Content-Type", "application/json")],
    [("x-ratelimit-remaining-requests", "not-a-number")],
])
def test_quota_ignores_missing_or_unparseable_headers(quota, headers):
    proxy._check_groq_quota(headers)
    assert quota.readouterr().out == ""


def test_transcribe_groq_reports_quota_from_a_real_response(fake_groq, quota):
    fake_groq.responses = [
        _FakeResponse(200, b'{"text":"ok"}', {"x-ratelimit-remaining-requests": "42"})
    ]
    status, _, _ = proxy._transcribe_groq(_FILE_INFO, language="en", prompt="")
    assert status == 200
    assert "Groq daily requests remaining: 42" in quota.readouterr().out


def test_transcribe_groq_gives_up_on_a_long_rate_limit(fake_groq, monkeypatch):
    """The plugin sends chunks serially, so a long sleep here stalls every chunk behind
    this one. Surfacing the 429 lets the next chunk start clean instead."""
    slept = []
    monkeypatch.setattr(backends.time, "sleep", slept.append)
    fake_groq.responses = [_FakeResponse(429, b'{"error":"rate limited"}', {"Retry-After": "60"})]

    status, _, _ = proxy._transcribe_groq(_FILE_INFO, language="en", prompt="")

    assert status == 429
    assert slept == []
    assert len(fake_groq.instances) == 1


# ---------------------------------------------------------------------------
# HTTP routes
#
# Added after the package split shipped a broken /conversations: the handler referenced
# names that were no longer imported, and every unit test passed because they all call the
# render/resolve functions directly. These start a real server and fetch real URLs.
# ---------------------------------------------------------------------------

def _serve(handler_cls):
    """Run the real handler on an ephemeral port for the duration of a test."""
    import http.server, threading, urllib.request
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def server(tmp_path, monkeypatch):
    # /identified-vessels serves a file from disk. Left pointing at the real one, this test
    # raced a running proxy rewriting it and timed out roughly one run in three. Patched on
    # `proxy`, not on vessel_log: do_GET reads the name imported into whisper-proxy's own
    # namespace, so patching the owning module would not be seen (see CONTRIBUTING.md).
    log = tmp_path / "identified_vessels.html"
    log.write_text("<html><body><table id='vessels'></table></body></html>", encoding="utf-8")
    monkeypatch.setattr(proxy, "VESSELS_LOG_FILE", str(log))
    srv, base = _serve(proxy.ProxyHandler)
    yield base
    srv.shutdown()


@pytest.mark.parametrize("path,expect", [
    ("/conversations", b"Resolved Conversations"),
    ("/api/conversations", b"["),
    ("/identified-vessels", b"<"),
    # Missing from this list, and so still broken by the same split: the handler read
    # _cache_lock/_vessel_cache as bare globals and answered 500.
    ("/api/ais-cache", b"["),
])
def test_get_routes_respond(server, path, expect):
    import urllib.request
    with urllib.request.urlopen(server + path, timeout=10) as r:
        assert r.status == 200
        assert expect in r.read()


# Every one of these is live state that changes second to second, and none of them carried
# a single cache directive -- no Cache-Control, no Expires, no ETag, no Last-Modified. A
# response with no freshness information at all may be cached heuristically, and /conversations
# self-refreshes with <meta http-equiv="refresh">, which is an ordinary navigation and so
# consults the HTTP cache. Observed directly: the server was serving 157 exchanges while the
# browser sat on 156, having reloaded on schedule and been handed its own cached copy.
@pytest.mark.parametrize("path", [
    "/conversations", "/api/conversations", "/identified-vessels", "/api/ais-cache",
])
def test_live_routes_forbid_caching(server, path):
    import urllib.request
    with urllib.request.urlopen(server + path, timeout=10) as r:
        assert "no-store" in r.headers.get("Cache-Control", "")


def test_unknown_post_path_is_rejected(server):
    import urllib.error, urllib.request
    req = urllib.request.Request(server + "/nope", data=b"x", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 404


from stt_proxy import conversation_correct as cc  # noqa: E402


def _window(when):
    return [
        {"id": 1, "time": when, "channel": "160,650",
         "text": "raw one", "corrected": "Maas Approach, motor vision Example Trader.",
         "live_vessel": None},
        {"id": 2, "time": when, "channel": "160,650",
         "text": "raw two", "corrected": "Motorvessel Example Trader, Maas Approach.",
         "live_vessel": None},
    ]


def test_storage_keeps_the_verbatim_text_beside_the_correction(monkeypatch, tmp_path):
    """The audit trail is the whole basis for allowing a rewrite at all."""
    when = datetime.datetime(2026, 8, 7, 10, 14, 15)
    monkeypatch.setattr(conversations, "_resolved", [])
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    corrections = {1: {"text": "Maas Approach, Motorvessel Example Trader.",
                       "changes": [{"from": "motor vision", "to": "Motorvessel",
                                    "reason": "shore station"}]}}
    conversations._store_resolved(
        _window(when),
        [{"chunk_ids": [1, 2], "vessel": "EXAMPLE TRADER", "mmsi": "1",
          "evidence": "e", "confidence": "high"}],
        corrections)
    turns = conversations._resolved[0]["turns"]
    assert turns[0]["text"] == "Maas Approach, motor vision Example Trader."
    assert turns[0]["conv"] == "Maas Approach, Motorvessel Example Trader."
    assert turns[0]["changes"][0]["to"] == "Motorvessel"
    assert "conv" not in turns[1], "an uncorrected turn stores no conv field"


def test_storage_without_corrections_is_unchanged(monkeypatch):
    when = datetime.datetime(2026, 8, 7, 10, 14, 15)
    monkeypatch.setattr(conversations, "_resolved", [])
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    conversations._store_resolved(
        _window(when),
        [{"chunk_ids": [1, 2], "vessel": None, "mmsi": None,
          "evidence": "e", "confidence": "low"}],
        None)
    turns = conversations._resolved[0]["turns"]
    assert "conv" not in turns[0]
    assert turns[0]["text"] == "Maas Approach, motor vision Example Trader."


def test_the_pass_does_not_run_while_the_flag_is_off(monkeypatch):
    """Default off: production behaviour must be byte-identical until the bake-off scores it."""
    def boom(*a, **k):
        raise AssertionError("correct_conversation must not be called with the flag off")
    monkeypatch.setattr(cc, "CONVERSATION_CORRECT", False)
    monkeypatch.setattr(cc, "correct_conversation", boom)
    monkeypatch.setattr(conversations, "resolve_conversation",
                        lambda w: [{"chunk_ids": [1, 2], "vessel": None, "mmsi": None,
                                    "evidence": "e", "confidence": "low"}])
    monkeypatch.setattr(conversations, "_resolved", [])
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    conversations._resolve_window(_window(datetime.datetime(2026, 8, 7, 10, 14, 15)))
    assert "conv" not in conversations._resolved[0]["turns"][0]


def test_a_failed_correction_still_stores_the_conversation(monkeypatch):
    """Never lose a conversation because a model misbehaved."""
    monkeypatch.setattr(cc, "CONVERSATION_CORRECT", True)
    monkeypatch.setattr(cc, "correct_conversation", lambda turns, vessel: None)
    monkeypatch.setattr(conversations, "resolve_conversation",
                        lambda w: [{"chunk_ids": [1, 2], "vessel": None, "mmsi": None,
                                    "evidence": "e", "confidence": "low"}])
    monkeypatch.setattr(conversations, "_resolved", [])
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    conversations._resolve_window(_window(datetime.datetime(2026, 8, 7, 10, 14, 15)))
    assert len(conversations._resolved) == 1
    assert conversations._resolved[0]["turns"][0]["text"]


def test_flag_on_success_reaches_storage_through_resolve_window(monkeypatch):
    """The wiring that matters: flag on, correction succeeds, the fix reaches storage.

    Calling _store_resolved directly (as the tests above do) bypasses the merge logic in
    _resolve_window entirely, so this drives the real entry point end to end.
    """
    monkeypatch.setattr(cc, "CONVERSATION_CORRECT", True)
    monkeypatch.setattr(
        cc, "correct_conversation",
        lambda turns, vessel: {1: {"text": "Maas Approach, Motorvessel Example Trader.",
                                    "changes": [{"from": "motor vision", "to": "Motorvessel",
                                                 "reason": "shore station"}]}})
    monkeypatch.setattr(conversations, "resolve_conversation",
                        lambda w: [{"chunk_ids": [1, 2], "vessel": "EXAMPLE TRADER", "mmsi": "1",
                                    "evidence": "e", "confidence": "high"}])
    monkeypatch.setattr(conversations, "_resolved", [])
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    conversations._resolve_window(_window(datetime.datetime(2026, 8, 7, 10, 14, 15)))
    turns = conversations._resolved[0]["turns"]
    assert turns[0]["conv"] == "Maas Approach, Motorvessel Example Trader."
    assert turns[0]["changes"][0]["to"] == "Motorvessel"
    assert "conv" not in turns[1]


def _window4(when):
    return [
        {"id": 1, "time": when, "channel": "160,650", "text": "raw one",
         "corrected": "one", "live_vessel": None},
        {"id": 2, "time": when, "channel": "160,650", "text": "raw two",
         "corrected": "two", "live_vessel": None},
        {"id": 3, "time": when, "channel": "160,650", "text": "raw three",
         "corrected": "three", "live_vessel": None},
        {"id": 4, "time": when, "channel": "160,650", "text": "raw four",
         "corrected": "four", "live_vessel": None},
    ]


def test_corrections_do_not_leak_across_exchanges(monkeypatch):
    """The per-exchange split exists so one conversation's context cannot edit another's
    turns. This is the assertion that would fail if someone "simplified" the loop in
    _resolve_window to one correction call per window instead of per exchange.
    """
    calls = []

    def record_call(turns, vessel):
        calls.append(sorted(t["id"] for t in turns))
        return None

    monkeypatch.setattr(cc, "CONVERSATION_CORRECT", True)
    monkeypatch.setattr(cc, "correct_conversation", record_call)
    monkeypatch.setattr(conversations, "resolve_conversation",
                        lambda w: [
                            {"chunk_ids": [1, 2], "vessel": "A", "mmsi": "1",
                             "evidence": "e", "confidence": "high"},
                            {"chunk_ids": [3, 4], "vessel": "B", "mmsi": "2",
                             "evidence": "e", "confidence": "high"},
                        ])
    monkeypatch.setattr(conversations, "_resolved", [])
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    conversations._resolve_window(_window4(datetime.datetime(2026, 8, 7, 10, 14, 15)))
    assert len(calls) == 2, "one correction call per exchange, never one per window"
    assert [1, 2] in calls, "exchange 1's call must see only its own turn ids"
    assert [3, 4] in calls, "exchange 2's call must see only its own turn ids"


def test_declared_no_changes_stores_no_conv_field(monkeypatch):
    """A correction that ran and declared no changes must not leak a conv/changes key --
    that key's presence is how the page tells "not corrected" apart from "corrected to the
    same thing", so an empty changes list must suppress it exactly like no correction at all.
    """
    when = datetime.datetime(2026, 8, 7, 10, 14, 15)
    monkeypatch.setattr(conversations, "_resolved", [])
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    corrections = {1: {"text": "Maas Approach, motor vision Example Trader.", "changes": []}}
    conversations._store_resolved(
        _window(when),
        [{"chunk_ids": [1, 2], "vessel": "EXAMPLE TRADER", "mmsi": "1",
          "evidence": "e", "confidence": "high"}],
        corrections)
    turns = conversations._resolved[0]["turns"]
    assert "conv" not in turns[0]
    assert "changes" not in turns[0]


def _row_with_correction():
    return [{
        "vessel": "EXAMPLE TRADER", "mmsi": "1", "confidence": "high", "evidence": "e",
        "channel": "160,650", "start": "2026-08-07 10:14:15", "end": "2026-08-07 10:14:19",
        "turns": [
            {"time": "10:14:15", "text": "Maas Approach, motor vision Example Trader.",
             "raw": "r", "live_vessel": None,
             "conv": "Maas Approach, Motorvessel Example Trader.",
             "changes": [{"from": "motor vision", "to": "Motorvessel",
                          "reason": "shore station rendition"}]},
            {"time": "10:14:19", "text": "Motorvessel Example Trader, Maas Approach.",
             "raw": "r", "live_vessel": None},
        ],
    }]


def test_the_page_shows_the_corrected_text():
    html = conversations.render_conversations_page(_row_with_correction())
    assert "Maas Approach, Motorvessel Example Trader." in html


def test_the_page_keeps_the_original_recoverable():
    """The rewrite was allowed on the condition that nothing is silently overwritten."""
    html = conversations.render_conversations_page(_row_with_correction())
    assert "motor vision" in html
    assert "shore station rendition" in html


def test_the_page_counts_the_corrections():
    html = conversations.render_conversations_page(_row_with_correction())
    assert "1 corrected" in html
    assert 'class="badge fixedcount"' in html


def test_the_page_promises_corrections_only_when_a_row_actually_carries_one():
    """With CONVERSATION_CORRECT off (or simply no correction on this page yet) no rendered
    row carries a 'conv' field, so the explanatory paragraph must not claim the page rewrites
    text -- that was true before this feature and must stay true when nothing here used it."""
    rows = _row_with_correction()
    for turn in rows[0]["turns"]:
        turn.pop("conv", None)
        turn.pop("changes", None)
    html = conversations.render_conversations_page(rows)
    assert "never rewrites it" in html
    assert "was corrected using" not in html


def test_the_page_promises_corrections_when_a_row_carries_one():
    html = conversations.render_conversations_page(_row_with_correction())
    assert "was corrected using" in html
    assert "never rewrites it" not in html


def test_one_windows_failure_does_not_lose_the_rest_of_the_batch(monkeypatch):
    """_take_closed_windows has already removed a closed window's chunks from the journal by
    the time the reaper resolves it, so one window blowing up (e.g. on the unhashable-id
    TypeError that validate_reply now turns into CorrectionRejected, or any other surprise)
    must not take the rest of the batch down with it -- every remaining window still gets
    stored."""
    when = datetime.datetime(2026, 8, 7, 10, 14, 15)
    good_window = _window(when)
    bad_window = [{"id": 99, "time": when, "channel": "x"}]
    monkeypatch.setattr(conversations, "_take_closed_windows", lambda: [bad_window, good_window])
    calls = []

    def fake_resolve(window):
        calls.append(window)
        if window is bad_window:
            raise TypeError("boom")

    monkeypatch.setattr(conversations, "_resolve_window", fake_resolve)
    conversations._reap_pass()
    assert calls == [bad_window, good_window], "the good window must still be reached"


def test_an_uncorrected_conversation_shows_no_badge_and_no_marked_text():
    """Assert on the badge markup and the marker class, NOT on the bare word 'fixedcount' or
    'corrected': the page's <style> block names the fixedcount class unconditionally (it is
    page-level, not per-row), and the static explanatory paragraph contains the word 'corrected'
    on every render. Only the actual badge/span markup tells corrected apart from uncorrected."""
    rows = _row_with_correction()
    for turn in rows[0]["turns"]:
        turn.pop("conv", None)
        turn.pop("changes", None)
    html = conversations.render_conversations_page(rows)
    assert 'class="badge fixedcount"' not in html
    assert 'class="fixed"' not in html
    assert "Maas Approach, motor vision Example Trader." in html
