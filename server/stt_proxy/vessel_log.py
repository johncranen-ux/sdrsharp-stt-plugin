"""The per-transmission HTML log at /identified-vessels.

Presentation only. Kept apart from identification so that changing how a row looks cannot
affect what gets identified.
"""

import datetime
import os
import threading

from stt_proxy.ais import _cache_size, _get_ship_type_name
from stt_proxy.markup import _html_escape, _vessel_link

_DEFAULT_VESSELS_LOG_FILE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "identified_vessels.html"))
VESSELS_LOG_FILE = os.path.normpath(
    os.environ.get("VESSELS_LOG_FILE", "").strip() or _DEFAULT_VESSELS_LOG_FILE)




def _init_vessels_log() -> None:
    if os.path.exists(VESSELS_LOG_FILE):
        return
    html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Identified Vessels</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        h1 { color: #333; }
        table { border-collapse: collapse; width: 100%; background: white; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        th { background: #2c3e50; color: white; padding: 12px; text-align: left; }
        td { padding: 10px 12px; border-bottom: 1px solid #ddd; }
        tr:hover { background: #f9f9f9; }
        .match { background: #d4edda; }
        .no-match { background: #fff3cd; }
    </style>
</head>
<body>
    <h1>Identified Vessels Log</h1>
    <table>
        <thead>
            <tr>
                <th>Timestamp</th><th>Vessel</th><th>MMSI</th><th>Callsign</th>
                <th>Type</th><th>AIS Type</th><th>IMO</th><th>Length</th>
                <th>Lat</th><th>Lon</th><th>Speed</th><th>Course</th><th>Transcription</th>
            </tr>
        </thead>
        <tbody id="vessels">
        </tbody>
    </table>
    <script>setInterval(() => location.reload(), 5000);</script>
</body>
</html>
"""
    try:
        with open(VESSELS_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as exc:
        print(f"[Vessels Log] init error: {exc}", flush=True)


_log_lock = threading.Lock()


def _append_vessel_to_log(result: dict, raw_text: str) -> None:
    try:
        # Every value below reaches the page from somewhere we do not control -- the
        # aisstream feed, the STT backend, or the extractor -- so none of it is trusted
        # into HTML raw. The vessel cell is a link out to VesselFinder when the AIS match
        # gave us an MMSI to look it up by.
        vessel   = _vessel_link(result["vessel"], result.get("mmsi")) if result.get("vessel") else "-"
        mmsi     = _html_escape(result.get("mmsi") or "-")
        callsign = _html_escape(result.get("callsign") or "-")
        vtype    = _html_escape(result.get("vessel_type") or "-")
        ais_type = _html_escape(_get_ship_type_name(result.get("type")) or "-")
        imo      = _html_escape(result.get("imo") or "-")
        length   = f"{result.get('length')}m" if result.get("length") else "-"
        lat      = f"{result.get('latitude'):.4f}" if result.get("latitude") is not None else "-"
        lon      = f"{result.get('longitude'):.4f}" if result.get("longitude") is not None else "-"
        speed    = f"{result.get('sog'):.1f}" if result.get("sog") is not None else "-"
        course   = f"{int(result.get('cog'))}" if result.get("cog") is not None else "-"
        ts       = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row_class = "match" if result.get("mmsi") else "no-match"
        preview   = _html_escape(raw_text[:80] + ("..." if len(raw_text) > 80 else ""))

        row = f"""        <tr class="{row_class}">
            <td>{ts}</td><td><strong>{vessel}</strong></td><td>{mmsi}</td><td>{callsign}</td>
            <td>{vtype}</td><td>{ais_type}</td><td>{imo}</td><td>{length}</td>
            <td>{lat}</td><td>{lon}</td><td>{speed}</td><td>{course}°</td>
            <td><em>{preview}</em></td>
        </tr>
"""
        with _log_lock:
            with open(VESSELS_LOG_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            content = content.replace('        <tbody id="vessels">', '        <tbody id="vessels">\n' + row)
            with open(VESSELS_LOG_FILE, "w", encoding="utf-8") as f:
                f.write(content)
    except Exception as exc:
        print(f"[Vessels Log] append error: {exc}", flush=True)
