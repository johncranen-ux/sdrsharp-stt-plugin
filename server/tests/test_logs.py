"""Bounded log reads. Log tailing crosses a network now; shipping a whole day per refresh
is not an option, and neither is a client that silently loses its place after rotation."""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.logs import latest_log, read_tail  # noqa: E402


def test_the_first_read_returns_the_end_of_the_file(tmp_path):
    log = tmp_path / "proxy-2026-08-18.log"
    log.write_bytes(b"x" * 1000 + b"the end\n")
    window = read_tail(log, offset=None, limit=16)
    assert window.text.endswith("the end\n")
    assert window.next_offset == log.stat().st_size


def test_reading_from_an_offset_returns_only_what_was_appended(tmp_path):
    # Bytes, not write_text: on Windows text mode turns every newline into CRLF, which would
    # move the offsets under the test without telling it. The reader works in bytes, so does this.
    log = tmp_path / "proxy-2026-08-18.log"
    log.write_bytes(b"first\n")
    first = read_tail(log, offset=0)
    log.write_bytes(b"first\nsecond\n")

    window = read_tail(log, offset=first.next_offset)
    assert window.text == "second\n"
    assert window.restarted is False


def test_a_truncated_or_rotated_file_restarts_at_the_beginning(tmp_path):
    log = tmp_path / "proxy-2026-08-18.log"
    log.write_bytes(b"a long first day\n")
    stale = read_tail(log, offset=0).next_offset
    log.write_bytes(b"new\n")

    window = read_tail(log, offset=stale)
    assert window.restarted is True
    assert window.text == "new\n"
    assert window.offset == 0


def test_a_read_is_bounded_however_large_the_file_and_the_limit(tmp_path):
    log = tmp_path / "proxy-2026-08-18.log"
    log.write_bytes(b"y" * 2_000_000)
    window = read_tail(log, offset=0, limit=10_000_000)
    assert len(window.text) <= 262_144
    assert window.next_offset < window.size


def test_a_missing_log_reads_as_empty_rather_than_raising(tmp_path):
    window = read_tail(tmp_path / "never-written.log")
    assert window.text == ""
    assert window.size == 0


def test_undecodable_bytes_do_not_break_a_read(tmp_path):
    """The proxy prints vessel names from AIS, which have arrived mis-encoded before."""
    log = tmp_path / "proxy-2026-08-18.log"
    log.write_bytes(b"before \xff\xfe after\n")
    assert "before" in read_tail(log, offset=0).text


def test_the_latest_log_is_the_most_recent_dated_file(tmp_path):
    for stamp in ("2026-08-16", "2026-08-18", "2026-08-17"):
        (tmp_path / f"proxy-{stamp}.log").write_text("x", encoding="utf-8")
    (tmp_path / "counter-2026-08-19.log").write_text("x", encoding="utf-8")
    assert latest_log(tmp_path, "proxy").name == "proxy-2026-08-18.log"
    assert latest_log(tmp_path, "nothing") is None
    assert latest_log(tmp_path / "no-such-dir", "proxy") is None
