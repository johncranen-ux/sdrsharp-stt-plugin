"""Text-level filtering and correction of transcriptions.

Everything here is a pure function of the text: no network, no shared state, no ordering
requirements. These are the cheap defences that run before anything expensive is done with
a transcription, and most of them exist because of something measured rather than assumed --
the comments say which.

Nothing in this module knows about vessels, AIS or the HTTP layer.
"""

import os
import re

from rapidfuzz import fuzz as rf_fuzz


# ---------------------------------------------------------------------------
# Hallucination filter
# ---------------------------------------------------------------------------

_HALLUCINATION_EXACT = {
    "you", "hmm", "hm", "ah", "uh", "um",
    "thank you", "thanks",
    "thank you for watching", "thanks for watching",
    "please subscribe", "subscribe",
    "bye", "goodbye",
}

_HALLUCINATION_PATTERNS = [
    re.compile(r'^\s*[.\s]+\s*$'),                   # only dots / whitespace
    re.compile(r'^\s*[\W\s]+\s*$'),                  # only punctuation
    re.compile(r'^(\w[\w\s]*?)\s*(\1\s*){3,}$', re.IGNORECASE),  # phrase repeated 4+ times
]


# Prompt echo
#
# Whisper sometimes reproduces the decoding prompt instead of transcribing, and the shipped
# prompt names a vessel ("Motortanker Neptune") that exists in AIS with a 100% match -- so an
# echo becomes a confidently wrong vessel identification with a real MMSI attached.
#
# Similarity to the prompt alone does NOT discriminate: real traffic genuinely says "Maas
# Approach" and "standing by on channel one six", and measured over 307 real transcripts the
# 95th percentile of partial_ratio against the prompt is 91. What does discriminate is
# (a) every word of the transmission coming from the prompt, plus (b) either enough words
# that coincidence is implausible, or a word distinctive to the prompt.
#
# Measured on those 307 transcripts: this flags 11, all verifiably echoes (verbatim prompt
# fragments, several repeated word-for-word), while leaving real short transmissions such as
# "This is Maas Approach." and "VHF channel six, over." alone.
#
# Set PROMPT_ECHO_FILTER=off to disable.
PROMPT_ECHO_FILTER    = os.environ.get("PROMPT_ECHO_FILTER", "on").strip().lower() != "off"
PROMPT_ECHO_MIN_WORDS = int(os.environ.get("PROMPT_ECHO_MIN_WORDS", "6"))

_WORD_TOKEN_RE = re.compile(r"[a-z0-9']+")

# Vocabulary that recurs naturally in Rotterdam VHF traffic. A prompt word outside this set
# (e.g. the prompt's invented vessel name and callsign) is treated as distinctive: seeing it
# in a transmission whose every word came from the prompt is strong evidence of an echo.
_ECHO_GENERIC_WORDS = frozenset("""
a an the this that is are be been am was were do does did have has had
i we you he she it they me us them my your our their
and or but if so then than to of in on at by for from with without over under
maas approach rotterdam vts pilot pilots botlek traffic harbour port
vhf channel one two three four five six seven eight nine zero
over out roger wilco copy standing standby say again please thank thanks sir
motortanker motorvessel tanker vessel ship boat barge tug
good morning afternoon evening day night yes yeah no okay ok
""".split())


def _prompt_echo_tokens(prompt: str) -> tuple[set, set]:
    """(all prompt words, distinctive prompt words) for echo detection."""
    words = set(_WORD_TOKEN_RE.findall(prompt.lower()))
    return words, words - _ECHO_GENERIC_WORDS


def _is_prompt_echo(text: str, prompt: str) -> bool:
    """True when `text` looks like the decoding prompt read back rather than speech."""
    if not PROMPT_ECHO_FILTER or not prompt:
        return False
    words = _WORD_TOKEN_RE.findall(text.lower())
    if not words:
        return False

    prompt_words, distinctive = _prompt_echo_tokens(prompt)
    # Every single word must come from the prompt. One novel word (a real vessel name, a
    # position, an instruction) means the speaker said something the prompt could not supply.
    if any(w not in prompt_words for w in words):
        return False

    return len(words) >= PROMPT_ECHO_MIN_WORDS or any(w in distinctive for w in words)


def _is_hallucination(text: str) -> bool:
    t = text.strip()
    if not t or len(t) < 2:
        return True
    t_lower = t.lower().rstrip('.,!?').strip()
    if t_lower in _HALLUCINATION_EXACT:
        return True
    for pat in _HALLUCINATION_PATTERNS:
        if pat.match(t):
            return True
    words = t_lower.split()
    if len(words) >= 4 and len(set(words)) == 1:
        return True
    return False


# ---------------------------------------------------------------------------
# Post-processing corrections
# ---------------------------------------------------------------------------

# Rules safe on any band. "Callsign" is standard aviation phraseology too.
_SHARED_CORRECTIONS = [
    (r'\bcosine\b', 'Callsign', re.IGNORECASE),
    (r'\bcall\s*sign\b', 'Callsign', re.IGNORECASE),
]

# Maritime-only: every one of these would be wrong or nonsensical on the aviation band
# ("draught" and "buoy" have no airband meaning, and "Maas" would corrupt legitimate
# approach names like "Rotterdam Approach" or "final approach").
_MARITIME_CORRECTIONS = [
    (r'\bmass\s+approach\b', 'Maas Approach', re.IGNORECASE),
    (r'\bmarch\s+approach\b', 'Maas Approach', re.IGNORECASE),
    (r'\bmars\s+approach\b', 'Maas Approach', re.IGNORECASE),
    (r'\bmass\b(?=\s)', 'Maas', re.IGNORECASE),
    (r'\bmars\b(?=\s)', 'Maas', re.IGNORECASE),
    (r'\bmotor\s+tanker\b', 'Motortanker', re.IGNORECASE),
    (r'\bdraft\b', 'draught', re.IGNORECASE),
    (r'\bboys\b', 'buoys', re.IGNORECASE),
    (r'\bboy\b', 'buoy', re.IGNORECASE),
    # "ladder" is the single most-mangled word in this traffic after the place names, and the
    # pilot boarding arrangement is read out in almost every exchange. Measured over the 636
    # benchmarked transmissions carrying a reference: the decoder produced "ladder" 38 times,
    # "letter" 14 and "leather" once, while the ground truth held "ladder" 15 times and
    # "letter" exactly once -- and that one was a typo in the reference (clip 0143, "pilot
    # letter port side", fixed with this change). Not one occurrence was a real letter, which
    # is what makes an unguarded substitution safe here; "boy" -> "buoy" above is the same
    # bet on the same grounds. Worth 0.14 pooled WER points on its own (36.83% -> 36.69%),
    # correcting 14 transmissions and damaging none.
    #
    # The "of" guard is precautionary, not measured: no "letter of ..." occurs in the corpus,
    # but a letter of protest and a letter of credit are real ship's business, and excluding
    # them costs nothing on the 14 cases that do occur.
    (r'\b(?:letter|leather)\b(?!\s+of\b)', 'ladder', re.IGNORECASE),
    # "at anchor" and "heave up anchor" come out as the Cambodian temple. 2 clips, ~0.10 WER
    # points; "Angkor" has no other meaning on this channel.
    (r'\bangkor\b', 'anchor', re.IGNORECASE),
]

# Fuzzy "<something> Approach" -> "Maas Approach".
#
# Measured necessity: the fixed regex rules above were derived from whisper.cpp's
# substitutions, which were consistent (mass/mars/march, over and over). Groq gets the
# same word wrong far more diversely -- 27 instances across 13 spellings on one 61-clip
# set (Aas, AAS, Aps, A.M.A.S.S., MAAAS, Ameas, Moth, MOTR, Master, ...). Hand-written
# rules do not survive that: on a held-out half they were worth 0.3 WER points, against
# 1.6 in-sample. Similarity matching generalises to spellings never seen during
# derivation and measured 3.7 points on the same held-out half.
#
# It was still firing on well under half the cases it should, for two independent reasons,
# each worth more than every hand-written rule above combined.
#
# 1. A recognised approach-word is a precondition, so a spelling the pattern missed took the
#    Maas correction down with it. "Aas Aapproach" was left entirely alone despite "Aas"
#    scoring 85.7 -- the leading double 'a' defeats `ap+`. 7 clips carried "Aapproach" and
#    one "Proach", and none of them could ever be corrected.
#
# 2. The threshold recognised only half the variants the references show. Measured against
#    "maas": aps 57.1, master 60.0, marsh 66.7, mots/must/last/mous 50.0 -- all of them
#    verifiably "Maas Approach" in the references, all of them left alone at 70.
#
# What licenses a threshold this loose is positional: across every reference file the token
# before an "approach" NOUN is "maas" 210 times out of 212, and the two exceptions are
# comma-separated, which the pattern already refuses to cross. Measured over the 636
# benchmarked transmissions, 50 corrects 54 rows across 27 clips and damages none
# (36.69% -> 35.45% pooled), and split-half validates at -1.04 and -1.51 rather than
# collapsing, because a similarity rule generalises where a list of spellings does not.
#
# Replacing *whatever* precedes the noun -- ignoring similarity entirely -- scores better
# still (35.34%), and is rejected. Clip 0037 is "Starfighter, Maas Approach" with the comma
# lost in decoding: a positional rule rewrites that to "Maas Approach" and deletes the ship.
# 0.11 WER points is not worth feeding the identification path a transmission with the vessel
# name removed. At 50 "Starfighter" scores 13.3 and is left alone.
#
# Only the noun is ever the station: every "approaching" in the references is ordinary
# English ("we are approaching", "I'm approaching"). The old rule replaced the whole word
# including its suffix, so "mass approaching" came back as "Maas Approach".
_APPROACH_RE = re.compile(r"\b([A-Za-z.']{1,12})(\s+)(a?ap*r?oa?ch\w*|proach\w*)", re.IGNORECASE)
MAAS_FUZZ_THRESHOLD = int(os.environ.get("MAAS_FUZZ_THRESHOLD", "50"))

# "Maas Center, Recon buoy" is read out about as often as the approach call and attracts the
# same mis-spellings ("Maaf Center, Rekkenbooi"). Thin evidence next to the approach rule --
# 2 clips, worth ~0.08 WER points -- but it is the identical correction on the identical
# word, so it shares the threshold rather than acquiring one of its own.
_CENTRE_RE = re.compile(r"\b([A-Za-z.']{1,12})(\s+)(cent[er][er])\b", re.IGNORECASE)


def _looks_like_maas(word: str) -> bool:
    stripped = word.lower().replace(".", "")
    return stripped == "maas" or rf_fuzz.ratio(stripped, "maas") >= MAAS_FUZZ_THRESHOLD


def _correct_maas_before_approach(text: str) -> str:
    def repl(match):
        # The verb is ordinary English; only the noun is ever the station.
        if match.group(3).lower().endswith("ing"):
            return match.group(0)
        if _looks_like_maas(match.group(1)):
            return f"Maas{match.group(2)}Approach"
        return match.group(0)

    return _APPROACH_RE.sub(repl, text)


def _correct_maas_before_centre(text: str) -> str:
    def repl(match):
        if _looks_like_maas(match.group(1)):
            return f"Maas{match.group(2)}Center"
        return match.group(0)

    return _CENTRE_RE.sub(repl, text)


def _apply_sttt_corrections(text: str, mode: str = "maritime") -> str:
    """Apply STT corrections appropriate to the band.

    Mode-scoped because these rules are not band-neutral: firing the maritime set on
    aviation traffic would rewrite "final approach" as "Maas Approach" and "draft" as
    "draught".
    """
    corrections = list(_SHARED_CORRECTIONS)
    if mode != "airband":
        corrections += _MARITIME_CORRECTIONS

    result = text
    for pattern, replacement, flags in corrections:
        result = re.sub(pattern, replacement, result, flags=flags)
    if mode != "airband":
        result = _correct_maas_before_approach(result)
        result = _correct_maas_before_centre(result)
    return result


# ---------------------------------------------------------------------------
# Claude vessel-extraction agent
# ---------------------------------------------------------------------------


# "Help Trader Maas Approach." produced PE2026. Both are real entries in the AIS callsign
# table, so match_by_callsign confirms them and a fabrication acquires an MMSI.
#
# A callsign is spoken by spelling it out, so a genuine one can be reconstructed from the
# words. Anything that cannot be is discarded. The asymmetry is deliberate: dropping a real
# callsign costs some enrichment, while keeping an invented one puts false identity on screen.
# A spelling this table does not know is worse than it looks: the run breaks in half, so the
# letters either side are lost too. "Oscar Whiskey Gulf Juliet two" decoded to ['OW', 'J2']
# rather than ['OWGJ2'], and MONA SWAN (MMSI 219624000, cs OWGJ2) went unidentified with its
# callsign spelled out twice in the conversation. "gulf" is how the letter is widely said on
# an international channel, and "x-ray" is simply its ordinary written form -- the hyphen
# alone was enough to break that one.
#
# Variants are added only where a real transmission produced them, because every addition
# widens the decoder, and the guard below is the last thing standing between a mis-heard word
# and a false identity on screen. Fuzzy-matching the whole table was measured and rejected:
# against the 07-28 corpus, no threshold separates "gulf"/"golf" (ratio 75) from "the"/"three"
# and "to"/"two", which score the same.
_PHONETIC_LETTERS = {
    "alpha": "A", "alfa": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E",
    "foxtrot": "F", "golf": "G", "gulf": "G", "hotel": "H", "india": "I", "juliet": "J",
    "juliett": "J", "x-ray": "X",
    "kilo": "K", "lima": "L", "mike": "M", "november": "N", "oscar": "O", "papa": "P",
    "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T", "uniform": "U", "victor": "V",
    "whiskey": "W", "whisky": "W", "xray": "X", "yankee": "Y", "zulu": "Z",
}
_SPOKEN_DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9",
}
_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def _decode_spoken_word(word: str) -> str | None:
    """The characters a single spoken word stands for, or None if it is ordinary speech."""
    char = _PHONETIC_LETTERS.get(word) or _SPOKEN_DIGITS.get(word)
    if char is None and word.isalnum():
        # Already-compact forms the decoder sometimes emits whole ("9HF5093"), and
        # single spoken characters ("9 Hotel Alpha").
        if len(word) == 1 or (len(word) <= 8 and any(c.isdigit() for c in word)
                              and any(c.isalpha() for c in word)):
            char = word.upper()
    return char


def _spelled_out_runs(text: str) -> list[str]:
    """Unbroken runs of spelled-out characters: phonetic letters, spoken digits, literals."""
    runs, current = [], []
    for word in re.findall(r"[A-Za-z0-9'-]+", (text or "").lower()):
        char = _decode_spoken_word(word)
        if char:
            current.append(char)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def _callsign_supported_by_text(callsign: str, text: str) -> bool:
    """True only if `callsign` can be read out of `text`."""
    wanted = _ALNUM_RE.sub("", callsign or "").upper()
    if len(wanted) < 3:
        return False
    if wanted in _ALNUM_RE.sub("", text or "").upper():
        return True
    return any(wanted in run for run in _spelled_out_runs(text))


# Partial callsigns
#
# A callsign survives STT only partly: "five Lima Romeo Kilo nine" (5LRK9) came through as
# "five DEMA Romeo, clear nine", so the exact lookup -- a dictionary hit, no fuzz -- could
# never fire, and the vessel went unidentified with its callsign spelled out twice.
#
# What the decoder can still recover is an ordered set of known characters plus the gaps
# between them, on the assumption that each unreadable word was one spoken character. That
# yields "5.R.9", which matches exactly one cached callsign.
#
# The keyword anchor is what makes this safe. Scanning the whole transmission picks up the
# "eight" in "MSC DEMA eight" and yields "8.5.R.9", which is wrong. Every spelled-out
# callsign in the reference corpus says "callsign" first, so requiring it costs little and
# bounds the span to something that really is a callsign.
_CALLSIGN_ANCHOR_RE = re.compile(r"\bcall\s?signs?\b", re.IGNORECASE)

PARTIAL_CALLSIGN_MIN_KNOWN = 3   # fewer characters than this does not discriminate
PARTIAL_CALLSIGN_MAX_LEN   = 7   # ITU callsign maximum
PARTIAL_CALLSIGN_MAX_GAP   = 2   # consecutive wildcards; beyond this the pattern is noise


def _partial_callsign_pattern(text: str) -> tuple[str, int] | None:
    """Regex for a partly-decodable spelled-out callsign, plus how many characters are known.

    None when the text carries no usable callsign span. Returns None for a *fully* decoded
    callsign too: that is the exact lookup's job, and this path exists only for the partial
    case.
    """
    match = _CALLSIGN_ANCHOR_RE.search(text or "")
    if not match:
        return None

    words   = re.findall(r"[A-Za-z0-9'-]+", text[match.end():].lower())
    decoded = [_decode_spoken_word(w) for w in words]

    first = next((i for i, c in enumerate(decoded) if c), None)
    if first is None:
        return None
    last = max(i for i, c in enumerate(decoded) if c)
    span = decoded[first:last + 1]

    if all(c for c in span):          # nothing was garbled -- not this function's problem
        return None

    gap = worst_gap = 0
    for char in span:
        gap = 0 if char else gap + 1
        worst_gap = max(worst_gap, gap)
    if worst_gap > PARTIAL_CALLSIGN_MAX_GAP:
        return None

    known  = sum(len(c) for c in span if c)
    length = sum(len(c) if c else 1 for c in span)
    if known < PARTIAL_CALLSIGN_MIN_KNOWN or length > PARTIAL_CALLSIGN_MAX_LEN:
        return None

    return "".join(re.escape(c) if c else "." for c in span), known
