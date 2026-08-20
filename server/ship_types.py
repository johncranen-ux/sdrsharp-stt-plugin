"""AIS ship-type codes, per ITU-R M.1371 (AIS message 5 / 24 static data).

One table, two consumers: the proxy names a vessel's type in its log, in the resolver's
candidate hints and in stored conversations; the control panel shows the raw code on the
Vessels screen and needs the same meanings for its tooltips. Keeping it here, importable with
no side effects, is what stops those two drifting apart.

They had already drifted from the standard itself. Until 2026-08-20 the proxy's own table was
shifted by ten across the whole 60-99 block and scrambled between 33 and 43: 704 cargo ships
read as "Tanker", 753 tankers read as "General cargo", 132 other-type vessels read as
"Container ship", and codes 79, 69, 99 and 55 fell through to a bare "Type 79". It also
carried 100-105 as "Bulk carrier", which is not an AIS code at all and matched nothing. That
error reached the resolver as well as the display -- see coarse_name below.

Two levels of name, deliberately:

- `coarse_name` is the category, and is what the log, the resolver's prompt hints and the
  plausibility scoring use. It stays stable across a hazard digit, because "Tanker carrying
  hazardous category B" is a tanker for every purpose those three have.
- `describe` is the full reading of the code, for a reader hovering over it. That is the only
  place the hazard category and the reserved ranges are worth spelling out.

Structure of the 60-99 block, which is why it is generated rather than typed out: the first
digit is the ship type and the second qualifies the cargo -- x0 all ships of this type, x1-x4
hazardous categories A-D, x5-x8 reserved, x9 no additional information.
"""
from __future__ import annotations

# The second digit's meaning in the 20-29, 40-49 and 60-99 blocks.
_QUALIFIER = {
    0: "all ships of this type",
    1: "carrying hazardous material, category A",
    2: "carrying hazardous material, category B",
    3: "carrying hazardous material, category C",
    4: "carrying hazardous material, category D",
    5: "reserved for future use",
    6: "reserved for future use",
    7: "reserved for future use",
    8: "reserved for future use",
    9: "no additional information",
}

# code -> (coarse category, full description). Codes outside these blocks are irregular and
# are simply listed.
_SPECIFIC: dict[int, tuple[str, str]] = {
    0: ("Not available", "Not available (default)"),
    29: ("SAR aircraft", "Search and rescue aircraft"),
    30: ("Fishing", "Fishing vessel"),
    31: ("Towing", "Towing vessel"),
    32: ("Towing", "Towing vessel, longer than 200 m or wider than 25 m"),
    33: ("Dredging/underwater ops", "Dredging or underwater operations"),
    34: ("Diving ops", "Diving operations"),
    35: ("Military ops", "Military operations"),
    36: ("Sailing", "Sailing vessel"),
    37: ("Pleasure craft", "Pleasure craft"),
    50: ("Pilot vessel", "Pilot vessel"),
    51: ("Search & rescue", "Search and rescue vessel"),
    52: ("Tug", "Tug"),
    53: ("Port tender", "Port tender"),
    54: ("Anti-pollution", "Vessel with anti-pollution equipment"),
    55: ("Law enforcement", "Law enforcement vessel"),
    56: ("Local vessel", "Local vessel (assigned by a local authority)"),
    57: ("Local vessel", "Local vessel (assigned by a local authority)"),
    58: ("Medical transport", "Medical transport"),
    59: ("Noncombatant", "Noncombatant vessel, per RR Resolution No. 18"),
}

# first digit -> (coarse category, how the description opens)
_BLOCKS = {
    2: ("Wing in ground", "Wing in ground (WIG) craft"),
    4: ("High-speed craft", "High-speed craft (HSC)"),
    6: ("Passenger", "Passenger ship"),
    7: ("Cargo", "Cargo ship"),
    8: ("Tanker", "Tanker"),
    9: ("Other", "Other type of ship"),
}


def _build() -> dict[int, tuple[str, str]]:
    table: dict[int, tuple[str, str]] = {}
    for code in range(0, 100):
        block, qualifier = divmod(code, 10)
        if block in _BLOCKS:
            coarse, opening = _BLOCKS[block]
            table[code] = (coarse, f"{opening} — {_QUALIFIER[qualifier]}")
        else:
            # 1-19 and 38, 39 have no meaning assigned; the specific codes above win.
            table[code] = ("Reserved", "Reserved for future use")
    table.update(_SPECIFIC)
    return table


_TABLE = _build()


def coarse_name(code) -> str | None:
    """The type category, or None when nothing was broadcast.

    This feeds the proxy log, the resolver's candidate hints and the plausibility tie-break, so
    it stays stable across a hazard digit: a tanker carrying category B is still a tanker to all
    three. An unknown code returns "Type <n>" rather than None -- the distinction between "no
    type" and "a type this table does not know" is worth keeping visible.
    """
    if code is None:
        return None
    entry = _TABLE.get(_as_code(code))
    return entry[0] if entry else f"Type {code}"


def describe(code) -> str | None:
    """The full reading of the code, for a tooltip. None when nothing was broadcast."""
    if code is None:
        return None
    normalised = _as_code(code)
    entry = _TABLE.get(normalised)
    if not entry:
        return f"Unknown AIS ship type code {code}"
    return f"{entry[1]} (AIS type {normalised})"


def _as_code(code) -> int | None:
    """AIS type arrives as an int from aisstream and as a string from AISHub's JSON."""
    try:
        return int(code)
    except (TypeError, ValueError):
        return None
