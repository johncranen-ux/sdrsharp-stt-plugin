"""Tests for whisper-proxy.py: hallucination filtering, STT corrections, and the
multipart parse/rebuild that lets the proxy own the whisper.cpp decoder parameters.

Run with: py -m pytest server/tests -v
"""

import datetime
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


@pytest.fixture
def ais_cache(monkeypatch):
    cache = {
        "GOOD WAY":       {"name": "GOOD WAY", "mmsi": "538010145"},
        "WILSON DURNESS": {"name": "WILSON DURNESS", "mmsi": "314632000"},
        "SYNTHESE 11":    {"name": "SYNTHESE 11", "mmsi": "111111111"},
        "AFTER YOU":      {"name": "AFTER YOU", "mmsi": "222222222"},
    }
    monkeypatch.setattr(proxy, "_vessel_cache", cache)
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
    monkeypatch.setattr(proxy, "AIS_HINT_FILTER", False)
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
    monkeypatch.setattr(proxy, "AIS_HINT_FILTER", False)
    assert proxy._hint_probes(text) == _legacy_probes(text)


# ---------------------------------------------------------------------------
# Prompt echo
# ---------------------------------------------------------------------------

_PROMPT = proxy.DEFAULT_MARITIME_PROMPT


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


def test_prompt_echo_filter_can_be_disabled(monkeypatch):
    monkeypatch.setattr(proxy, "PROMPT_ECHO_FILTER", False)
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
    monkeypatch.setattr(proxy, "_vessel_buffer", entries)
    return entries


def test_session_includes_turns_within_the_gap(buffer):
    buffer.extend([_turn(20, "WILSON DURNESS"), _turn(10, None, shore=True), _turn(2)])
    assert len(proxy._session_turns("160,650")) == 3


def test_session_stops_at_a_silence_longer_than_the_gap(buffer):
    buffer.extend([_turn(400, "OLD VESSEL"), _turn(20, "WILSON DURNESS"), _turn(2)])
    turns = proxy._session_turns("160,650")
    assert [t["vessel"] for t in turns] == ["WILSON DURNESS", None]


def test_session_is_scoped_to_one_channel(buffer):
    buffer.extend([_turn(10, "OTHER", channel="161,650"), _turn(2, "WILSON DURNESS")])
    assert [t["vessel"] for t in proxy._session_turns("160,650")] == ["WILSON DURNESS"]


def test_session_vessel_counts_shore_turns_too():
    """When shore speaks it addresses the vessel by name, so that turn identifies the
    counterpart. Also, _is_maas_response flags a vessel *calling* Maas Approach as shore,
    so filtering identity on that flag would discard the clearest identification there is."""
    turns = [_turn(20, "WILSON DURNESS", "Maas Approach, Wilson Durness", shore=True),
             _turn(10, "WILSON DURNESS", "Wilson Durness, Maas Approach", shore=True)]
    assert proxy._session_vessel(turns) == "WILSON DURNESS"


def test_session_vessel_is_none_when_no_turn_identified_anyone():
    assert proxy._session_vessel([_turn(20), _turn(10, shore=True)]) is None


def test_session_vessel_is_none_when_two_vessels_disagree():
    turns = [_turn(20, "WILSON DURNESS"), _turn(10, "MSC PANTERA")]
    assert proxy._session_vessel(turns) is None



# ---------------------------------------------------------------------------
# Conversation windowing
# ---------------------------------------------------------------------------

def _chunk(seconds_ago, text="", channel="160,650", cid=None, callsign=None, vessel=None):
    return {
        "id": cid if cid is not None else seconds_ago,
        "time": datetime.datetime.now() - datetime.timedelta(seconds=seconds_ago),
        "channel": channel, "text": text, "callsign": callsign,
        "live_vessel": vessel, "live_mmsi": None,
    }


@pytest.fixture
def journal(monkeypatch):
    entries = []
    monkeypatch.setattr(proxy, "_conversation_chunks", entries)
    return entries


def test_split_windows_breaks_on_a_long_silence():
    windows = proxy._split_windows([_chunk(300), _chunk(200), _chunk(20), _chunk(10)])
    assert [len(w) for w in windows] == [1, 1, 2]


def test_split_windows_keeps_a_continuous_exchange_together():
    windows = proxy._split_windows([_chunk(50), _chunk(40), _chunk(30), _chunk(20)])
    assert len(windows) == 1


def test_split_windows_caps_window_size(monkeypatch):
    """Bounds the resolver prompt: a busy channel must not build one huge window."""
    monkeypatch.setattr(proxy, "CONVERSATION_MAX_CHUNKS", 3)
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
    monkeypatch.setattr(proxy, "_callsign_cache", {"PABC": _CANDIDATES["SERENADA"]})
    monkeypatch.setattr(proxy, "_vessel_cache", {})
    cands = proxy._resolver_candidates([_chunk(10, "callsign papa alpha bravo charlie", callsign="PABC")])
    assert cands[0]["name"] == "SERENADA" and cands[0]["via_callsign"] is True


@pytest.mark.parametrize("text,expected", [
    ("Maas Approach, Callsign nine Hotel Alpha six one", True),
    ("this is call sign PABC", True),
    ("Zulu Charlie Foxtrot seven, over", True),          # three phonetics, no cue word
    ("Gungor Star one three one five, correct.", False),  # produced VRSQ4 live: invented
    ("Help Trader Maas Approach.", False),                # produced PE2026 live: invented
    ("Maas Approach, Maas Approach, Wilson Durness.", False),
])
def test_states_a_callsign(text, expected):
    assert proxy._states_a_callsign(text) is expected


def test_invented_callsigns_are_not_promoted_to_evidence(monkeypatch):
    """Measured: the live pass emits callsigns for transmissions containing none, and they
    can hit the AIS table exactly. Marking those 'via callsign' would launder a guess."""
    monkeypatch.setattr(proxy, "_callsign_cache", {"VRSQ4": {"name": "COSCO SHIPPING STAR", "mmsi": "1"}})
    monkeypatch.setattr(proxy, "_vessel_cache", {})
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


# ---------------------------------------------------------------------------
# Stored conversations and the page
# ---------------------------------------------------------------------------

def test_stored_turn_text_is_copied_verbatim_from_the_journal(monkeypatch):
    """The whole point of resolving afterwards: transcriptions must be untouched."""
    saved = []
    monkeypatch.setattr(proxy, "_resolved", saved)
    monkeypatch.setattr(proxy, "_save_conversations", lambda: None)
    window = [_chunk(30, "Maas Approach, Selenada.", cid=1), _chunk(20, "Roger, over.", cid=2)]
    original = [c["text"] for c in window]

    proxy._store_resolved(window, [{"chunk_ids": [1, 2], "vessel": "SERENADA", "mmsi": "275545000",
                                    "evidence": "later turn", "confidence": "high"}])

    assert [t["text"] for t in saved[0]["turns"]] == original
    assert [c["text"] for c in window] == original, "resolution must not mutate the journal"


def test_page_renders_with_no_conversations():
    assert "No conversations resolved yet" in proxy.render_conversations_page([])


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


@pytest.mark.parametrize("spelling", ["approach", "Approach", "Aproach"])
def test_fuzzy_maas_tolerates_misspelled_approach(spelling):
    """Only spellings actually observed in Groq output -- 'Approach' 25x, 'approach' 14x,
    'Aproach' 2x. The regex is deliberately not widened past what the data shows."""
    assert proxy._correct_maas_before_approach(f"Aas {spelling}").startswith("Maas Approach")


@pytest.mark.parametrize("text", [
    "Rotterdam Approach, this is Neptune",
    "cleared for final approach",
    "Schiphol approach, good morning",
])
def test_fuzzy_maas_leaves_dissimilar_names_alone(text):
    assert proxy._correct_maas_before_approach(text) == text


def test_fuzzy_maas_is_applied_through_the_maritime_pipeline():
    assert "Maas Approach" in proxy._apply_sttt_corrections("AAS approach, AAS approach, Fjordstrom")


def test_fuzzy_maas_is_not_applied_on_airband():
    text = "Aas approach"
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
    monkeypatch.setattr(proxy, "_quota_last_bucket", None)
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
    monkeypatch.setattr(proxy.time, "sleep", slept.append)
    fake_groq.responses = [_FakeResponse(429, b'{"error":"rate limited"}', {"Retry-After": "60"})]

    status, _, _ = proxy._transcribe_groq(_FILE_INFO, language="en", prompt="")

    assert status == 429
    assert slept == []
    assert len(fake_groq.instances) == 1
