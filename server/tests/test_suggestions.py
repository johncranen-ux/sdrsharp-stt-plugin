"""Tests for the sub-cutoff suggestion shortlist.

What this feature is, and what it deliberately is not: when the resolver names nobody, the
page offers the best two or three name matches it found BELOW the identification cutoff, as
a shortlist for the reader to adjudicate by ear. It never asserts one of them.

Measured on the 08-13/14 labels before it was built: of the 35 conversations the resolver
left unidentified, the right ship is in a three-item shortlist 9 times. Reusing the live
retrieval and relaxing its cutoff manages 3, because two cargo ships named MAAS and MAS take
56 of the 105 top-three slots -- matched against the shore station's own callout. Removing
them is what the document-frequency filter is for, and `test_a_probe_heard_in_most_...`
below is that finding turned into a regression test.
"""

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import ais                     # noqa: E402
from stt_proxy import conversations as conv   # noqa: E402


@pytest.fixture
def cache(monkeypatch):
    """A small vessel cache, installed the way the module's own loader leaves it."""
    def install(entries):
        by_name = {e["name"].upper(): e for e in entries}
        monkeypatch.setattr(ais, "_vessel_cache", by_name)
        monkeypatch.setattr(ais, "AIS_MAX_AGE_MIN", 0)
        return by_name
    return install


def _v(name, mmsi, **extra):
    return {"name": name, "mmsi": mmsi, **extra}


# ---------------------------------------------------------------------------
# ais.suggest_vessels -- retrieval
# ---------------------------------------------------------------------------

def test_the_best_matching_vessel_is_ranked_first(cache):
    cache([_v("MELTEMI I", "111"), _v("NOORDBORG", "222")])
    got = ais.suggest_vessels("Maas Approach, meld them in", n=2)
    assert got[0]["name"] == "MELTEMI I"


def test_a_suggestion_reports_the_fragment_it_matched(cache):
    """The whole point of the block: the reader adjudicates by ear, so they need the words."""
    cache([_v("MELTEMI I", "111")])
    got = ais.suggest_vessels("Maas Approach, meld them in", n=1)
    assert got[0]["heard"] == "MELD THEM IN"


def test_a_suggestion_carries_the_score_it_was_ranked_on(cache):
    cache([_v("MELTEMI I", "111")])
    got = ais.suggest_vessels("Maas Approach, meld them in", n=1)
    assert 70 <= got[0]["score"] <= 80


def test_no_more_than_n_suggestions_are_returned(cache):
    cache([_v(f"BALTIC {i}", str(i)) for i in range(10)])
    assert len(ais.suggest_vessels("motor vessel baltic for", n=3)) == 3


def test_a_vessel_scoring_below_the_floor_is_not_suggested(cache):
    """Without a floor every conversation gets three names, however unlike anything heard."""
    cache([_v("QUEEN MARY 2", "111")])
    assert ais.suggest_vessels("Maas Approach, roger that, out", floor=55) == []


def test_one_mmsi_is_suggested_once_even_under_two_names(cache):
    """Two cache rows can carry the same MMSI. Offering the reader the same ship twice
    wastes a slot in a list only three long."""
    cache([_v("SEA BANCKERT", "111"), _v("SEA BANCKERD", "111"), _v("SEA RANGER", "222")])
    got = ais.suggest_vessels("motor vessel sea banker", n=3)
    assert [s["mmsi"] for s in got] == ["111", "222"]


def test_a_filtered_probe_cannot_produce_a_suggestion(cache):
    """The hook the document-frequency filter hangs on."""
    cache([_v("MAAS", "111"), _v("MELTEMI I", "222")])
    got = ais.suggest_vessels("Maas Approach, meld them in", n=3,
                              probe_filter=lambda p: "MAAS" not in p)
    assert "111" not in [s["mmsi"] for s in got]


# Breaking the ties
#
# GT VELA, 2026-08-18: with the stale wrong answer removed, FIVE candidates tied at exactly
# 80.0 and the vessel actually calling was one of them. Ordering among equals is otherwise
# whatever the cache iterated in, so ranking them by plausibility costs nothing that score
# already decided -- it only replaces an arbitrary order with a defensible one.

def test_tied_scores_are_broken_by_proximity_to_maas_center(cache, monkeypatch):
    monkeypatch.setattr(ais, "SUGGEST_TIEBREAK", True)
    # Both sit one substitution from "BELLA", so both score exactly 80 -- which is the
    # premise. A name equal to the probe would win on score and test nothing.
    cache([_v("BOLLA", "111", latitude=52.30, longitude=3.60),      # ~28 km
           _v("BELLE", "222", latitude=52.03, longitude=3.90)])     # ~2 km
    got = ais.suggest_vessels("motor vessel bella", n=2)
    assert [s["score"] for s in got] == [got[0]["score"]] * 2, "the premise is a tie"
    assert got[0]["name"] == "BELLE"


def test_a_better_score_still_wins_over_a_nearer_ship(cache, monkeypatch):
    """The tie-break must not become a plausibility ranking -- score decides first."""
    monkeypatch.setattr(ais, "SUGGEST_TIEBREAK", True)
    cache([_v("MELTEMI I", "111", latitude=53.50, longitude=2.10),  # far, but a real match
           _v("MELD", "222", latitude=52.03, longitude=3.90)])      # near, weaker match
    assert ais.suggest_vessels("motor vessel meltemi one", n=1)[0]["name"] == "MELTEMI I"


def test_the_tiebreak_is_off_until_it_has_been_measured(cache):
    assert ais.SUGGEST_TIEBREAK is False


def test_empty_text_suggests_nothing(cache):
    cache([_v("MELTEMI I", "111")])
    assert ais.suggest_vessels("   ") == []


def test_an_empty_cache_suggests_nothing(cache):
    cache([])
    assert ais.suggest_vessels("Maas Approach, meld them in") == []


def test_suggesting_does_not_disturb_the_resolver_candidate_list(cache):
    """The shortlist must be a second, separate read. If it ever shares state with
    `_find_ais_hints`, a display-only feature has started changing what gets asserted."""
    cache([_v("MAAS", "111"), _v("MELTEMI I", "222")])
    before = ais._find_ais_hints("Maas Approach, meld them in")
    ais.suggest_vessels("Maas Approach, meld them in", n=3)
    assert ais._find_ais_hints("Maas Approach, meld them in") == before


# ---------------------------------------------------------------------------
# The document-frequency filter -- what keeps MAAS out of every shortlist
# ---------------------------------------------------------------------------

def _stored(*texts):
    return [{"turns": [{"text": t}]} for t in texts]


def _channel_corpus():
    """A corpus the size the filter is actually allowed to run on.

    Sized at SUGGEST_MIN_DOCS deliberately: at the 5% threshold a single mention is only
    rare enough to survive once there are 20 or more conversations, and the minimum-docs
    guard is what promises there are. A smaller corpus here would test a regime the code
    never runs in, and would call a one-off vessel name boilerplate.
    """
    return _stored(*(["Maas Approach, Maas Approach, good morning"] * (conv.SUGGEST_MIN_DOCS - 1)
                     + ["Maas Approach, motor vessel Meltemi"]))


def test_a_probe_heard_in_most_conversations_is_treated_as_boilerplate():
    """MAAS is in 93% of the 300 stored conversations. It is the station, not a ship."""
    assert conv._boilerplate_filter(_channel_corpus())("MAAS") is False


def test_a_probe_heard_in_one_conversation_survives_the_filter():
    assert conv._boilerplate_filter(_channel_corpus())("MELTEMI") is True


def test_the_filter_reads_corrected_text_when_a_turn_has_it():
    """The page shows corrected text, so the frequency table must count the same words."""
    rows = [{"turns": [{"text": "garble", "conv": "Maas Approach"}]}] * conv.SUGGEST_MIN_DOCS
    assert conv._boilerplate_filter(rows)("MAAS") is False


# ---------------------------------------------------------------------------
# Attaching suggestions to a resolved row
# ---------------------------------------------------------------------------

@pytest.fixture
def corpus(monkeypatch):
    """Enough stored conversations for the frequency table to mean something."""
    rows = [{"turns": [{"text": "Maas Approach, Maas Approach, good morning"}]}
            for _ in range(conv.SUGGEST_MIN_DOCS)]
    monkeypatch.setattr(conv, "_resolved", rows)
    return rows


def test_an_unidentified_row_gets_suggestions(cache, corpus):
    cache([_v("MELTEMI I", "111")])
    row = {"vessel": None, "mmsi": None,
           "turns": [{"text": "Maas Approach, meld them in"}]}
    conv._attach_suggestions(row)
    assert [s["name"] for s in row["suggestions"]] == ["MELTEMI I"]


def test_an_identified_row_ALSO_gets_suggestions(cache, corpus):
    """Reversed 2026-08-20. This used to assert the opposite.

    The original reasoning was that a named conversation is answered, and a shortlist beside
    it invites second-guessing an identification carrying evidence the shortlist does not
    have. LISTA/LISCA NERA M disproved the premise. At 14:42 the resolver named LISTA -- a
    ship three days stale -- because the single probe "LIST" scored 88.9 against it. The ship
    actually calling, LISCA NERA M, had been seen five minutes earlier and scored 78.3 on the
    probe "LIST CANERA", under the cutoff of 85. It was in the cache, it was fresh, it was
    correct, and the shortlist that would have surfaced it was suppressed precisely BECAUSE
    the wrong answer had been named confidently.

    A wrong confident answer is when the near misses are worth most, not least. Nothing here
    is asserted, so precision is untouched by construction.
    """
    cache([_v("MELTEMI I", "111")])
    row = {"vessel": "SOMETHING ELSE", "mmsi": "999",
           "turns": [{"text": "Maas Approach, meld them in"}]}
    conv._attach_suggestions(row)
    assert [s["name"] for s in row["suggestions"]] == ["MELTEMI I"]


def test_the_named_vessel_is_not_listed_among_its_own_near_misses(cache, corpus):
    """A slot spent restating the answer is a slot not spent on the alternative."""
    cache([_v("MELTEMI I", "111")])
    row = {"vessel": "MELTEMI I", "mmsi": "111",
           "turns": [{"text": "Maas Approach, meld them in"}]}
    conv._attach_suggestions(row)
    assert "111" not in [s["mmsi"] for s in row.get("suggestions") or []]


def test_excluding_the_named_vessel_does_not_shorten_the_list(cache, corpus):
    """Ask for one more than needed, so dropping the named ship still fills the shortlist."""
    cache([_v("MELTEMI I", "111"), _v("MELTEM", "222"), _v("MELDEM IN", "333"),
           _v("MELTEMI II", "444")])
    row = {"vessel": "MELTEMI I", "mmsi": "111",
           "turns": [{"text": "Maas Approach, meld them in"}]}
    conv._attach_suggestions(row)
    assert len(row["suggestions"]) == conv.SUGGEST_N


def test_an_identified_row_still_keeps_its_identification(cache, corpus):
    """The reversal must not touch what was named -- only what is offered beside it."""
    cache([_v("MELTEMI I", "111")])
    row = {"vessel": "SOMETHING ELSE", "mmsi": "999",
           "turns": [{"text": "Maas Approach, meld them in"}]}
    conv._attach_suggestions(row)
    assert row["vessel"] == "SOMETHING ELSE" and row["mmsi"] == "999"


def test_suggestions_never_name_the_vessel(cache, corpus):
    """The constraint this feature lives or dies by. A suggestion that reached `vessel`
    would be a confident misidentification wearing a shortlist's clothes -- and it is
    exactly how 'to Leland' once became 'Vlieland'."""
    cache([_v("MELTEMI I", "111")])
    row = {"vessel": None, "mmsi": None,
           "turns": [{"text": "Maas Approach, meld them in"}]}
    conv._attach_suggestions(row)
    assert row["suggestions"] and row["vessel"] is None and row["mmsi"] is None


def test_no_suggestions_before_the_corpus_can_say_what_is_boilerplate(cache, monkeypatch):
    """Cold start: with no stored conversations every probe looks rare, so the station's
    own name floods the shortlist. Showing nothing is the honest answer."""
    cache([_v("MAAS", "111"), _v("MELTEMI I", "222")])
    monkeypatch.setattr(conv, "_resolved", [])
    row = {"vessel": None, "mmsi": None,
           "turns": [{"text": "Maas Approach, meld them in"}]}
    conv._attach_suggestions(row)
    assert "suggestions" not in row


def test_the_feature_can_be_switched_off(cache, corpus, monkeypatch):
    cache([_v("MELTEMI I", "111")])
    monkeypatch.setattr(conv, "SUGGEST", False)
    row = {"vessel": None, "mmsi": None,
           "turns": [{"text": "Maas Approach, meld them in"}]}
    conv._attach_suggestions(row)
    assert "suggestions" not in row


def test_a_row_with_nothing_matchable_carries_no_empty_block(cache, corpus):
    cache([_v("QUEEN MARY 2", "111")])
    row = {"vessel": None, "mmsi": None, "turns": [{"text": "Roger that, out"}]}
    conv._attach_suggestions(row)
    assert "suggestions" not in row


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

SUGGESTED = [{"mmsi": "111", "name": "MELTEMI I", "score": 76.2, "heard": "MELD THEM IN"}]


def test_the_block_says_the_matches_are_below_the_cutoff():
    """Without the remark the reader has no way to tell a shortlist from an answer."""
    html = conv._format_suggestions({"suggestions": SUGGESTED})
    assert "below the identification cutoff" in html


def test_a_suggested_vessel_links_to_vesselfinder():
    html = conv._format_suggestions({"suggestions": SUGGESTED})
    assert 'href="https://www.vesselfinder.com/vessels/details/111"' in html


def test_the_block_shows_what_was_heard():
    html = conv._format_suggestions({"suggestions": SUGGESTED})
    assert "Meld Them In" in html


def test_a_row_without_suggestions_renders_no_block():
    """Every row stored before this feature existed lacks the key."""
    assert conv._format_suggestions({"vessel": None}) == ""


def test_a_suggested_name_is_escaped():
    """Vessel names come off the AIS feed, which anyone with a transmitter can write to."""
    html = conv._format_suggestions({"suggestions": [
        {"mmsi": "1", "name": '<script>x</script>', "score": 60.0, "heard": "X"}]})
    assert "<script>" not in html


def test_the_page_renders_the_block_for_an_unidentified_conversation():
    page = conv.render_conversations_page([{
        "vessel": None, "mmsi": None, "start": "2026-08-13 10:21:56",
        "end": "2026-08-13 10:22:26", "channel": "160,650",
        "turns": [{"time": "10:21:56", "text": "Maas Approach, meld them in"}],
        "suggestions": SUGGESTED,
    }])
    assert "below the identification cutoff" in page and "MELTEMI I" in page
