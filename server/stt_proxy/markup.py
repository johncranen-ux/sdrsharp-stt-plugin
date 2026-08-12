"""HTML shared by the two pages this server renders.

Small on purpose. It exists because /conversations and /identified-vessels both need to
escape untrusted values and link a vessel out to VesselFinder, and vessel_log.py is
"presentation only, kept apart from identification" -- so it must not import the resolver
just to reach a helper.

Everything rendered here is untrusted. Vessel names and callsigns come off whichever AIS
source is configured (AISHub by default, aisstream still selectable) and originate as
free-text fields any transmitter can inject into; transcriptions come from the STT backend.
None of it is authored by us, so all of it is escaped.
"""

from urllib.parse import quote


def _html_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# Looked up by MMSI rather than by name, even though a name would read better: vessel names
# are not unique -- a live snapshot of the Maas approach carries ALBATROS three times -- and
# the ones here have been through STT. The MMSI is the thing the AIS match actually
# established, so it is what resolves to the right ship.
#
# The details path rather than the search path: this is used where the reader is choosing
# between candidates, and a search result page makes them choose twice.
VESSELFINDER_URL = "https://www.vesselfinder.com/vessels/details/{mmsi}"


def _vessel_link(vessel: str, mmsi: str | None) -> str:
    """The vessel name, linked to its VesselFinder page when the MMSI is known.

    The MMSI is percent-encoded into the path and then escaped into the attribute -- never
    interpolated raw. Without an MMSI there is nothing to look up, so the name is returned
    as plain escaped text rather than as a link that would go nowhere useful.
    """
    name = _html_escape(vessel)
    if not mmsi:
        return name
    href = _html_escape(VESSELFINDER_URL.format(mmsi=quote(str(mmsi), safe="")))
    return f'<a class="vf" href="{href}" target="_blank" rel="noopener noreferrer">{name}</a>'
