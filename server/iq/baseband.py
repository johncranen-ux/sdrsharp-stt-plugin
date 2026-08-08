"""Reading SDR# baseband (raw IQ) recordings, and synthesising IQ for tests.

SDR#'s BasebandRecorder writes interleaved I/Q. Two things about the real captures shape
this module, and neither is optional:

* **They are RF64, not plain wav.** 60 min at 250 kSPS is ~3.6 GB and SDR#'s
  "WAV SDR# Compatible" format tops out at 2-4 GB, so the operator records "WAV RF64".
  RF64 replaces the RIFF magic, writes 0xFFFFFFFF where the 32-bit sizes would overflow,
  and puts the real 64-bit sizes in a `ds64` chunk. Python's `wave` module cannot read it,
  hence the chunk walker below.
* **They do not fit in memory.** 900M complex samples is 14.4 GB as complex128. Everything
  real goes through iter_baseband; read_baseband refuses anything large so that it can
  never quietly become the thing that exhausts RAM.

The sample rate is in the header; the CENTRE FREQUENCY is not, and lives only in the
filename. parse_centre_freq returns None rather than a default, because a guessed centre
would mistune every arm by the same amount -- a set of results that is self-consistent and
uniformly wrong.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

# Refuse to eagerly read more than this; real captures are gigabytes.
MAX_EAGER_BYTES = 256 * 1024 * 1024

# SDR# names recordings like SDRSharp_20260808_120000Z_160650000Hz_IQ.wav
_CENTRE_RE = re.compile(r"_(\d+)Hz", re.IGNORECASE)


@dataclass(frozen=True)
class BasebandInfo:
    rate: float
    centre_hz: float | None
    channels: int
    bits: int
    data_offset: int
    data_bytes: int

    @property
    def frames(self) -> int:
        return self.data_bytes // (self.channels * self.bits // 8)


def parse_centre_freq(filename: str) -> float | None:
    """The centre frequency SDR# encoded in the filename, or None if it is not there."""
    match = _CENTRE_RE.search(Path(filename).name)
    return float(match.group(1)) if match else None


def open_baseband(path: str | Path) -> BasebandInfo:
    """Parse a RIFF or RF64 header without touching the sample data."""
    path = Path(path)
    with open(path, "rb") as fh:
        magic = fh.read(4)
        if magic not in (b"RIFF", b"RF64"):
            raise ValueError(f"{path.name}: not a RIFF/RF64 file")
        fh.read(4)                                  # riff size, 0xFFFFFFFF for RF64
        if fh.read(4) != b"WAVE":
            raise ValueError(f"{path.name}: not a WAVE file")

        rate = channels = bits = 0
        data_offset = data_bytes = 0
        ds64_data_bytes: int | None = None

        while True:
            header = fh.read(8)
            if len(header) < 8:
                break
            chunk_id, size = struct.unpack("<4sI", header)
            body_at = fh.tell()

            if chunk_id == b"ds64":
                # riffSize, dataSize, sampleCount -- the real 64-bit sizes.
                _, ds64_data_bytes, _ = struct.unpack("<QQQ", fh.read(24))
            elif chunk_id == b"fmt ":
                fmt = fh.read(min(size, 16))
                _, channels, rate_i, _, _, bits = struct.unpack("<HHIIHH", fmt)
                rate = float(rate_i)
            elif chunk_id == b"data":
                data_offset = body_at
                data_bytes = size
                if size == 0xFFFFFFFF:
                    if ds64_data_bytes is None:
                        raise ValueError(f"{path.name}: RF64 data chunk with no ds64")
                    data_bytes = ds64_data_bytes
                break                                # data is last; stop before reading it

            fh.seek(body_at + size + (size & 1))      # chunks are word-aligned

    if channels != 2:
        raise ValueError(f"{path.name}: expected 2 channels (I/Q), got {channels}")
    if bits not in (8, 16, 32):
        raise ValueError(f"{path.name}: unsupported sample width {bits} bits")

    return BasebandInfo(rate, parse_centre_freq(path.name), channels, bits,
                        data_offset, data_bytes)


def _decode(raw: bytes, bits: int) -> np.ndarray:
    """Interleaved I/Q bytes -> complex128, matching SDR#'s three Sample Format options."""
    if bits == 16:
        flat = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif bits == 8:
        flat = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    else:
        flat = np.frombuffer(raw, dtype="<f4").astype(np.float64)
    return flat[0::2] + 1j * flat[1::2]


def iter_baseband(path: str | Path, block_frames: int = 1 << 20) -> Iterator[np.ndarray]:
    """Stream a capture as complex128 blocks. The only safe way to read a real recording."""
    info = open_baseband(path)
    frame_bytes = info.channels * info.bits // 8
    remaining = info.data_bytes
    with open(path, "rb") as fh:
        fh.seek(info.data_offset)
        while remaining > 0:
            want = min(block_frames * frame_bytes, remaining)
            raw = fh.read(want)
            if not raw:
                break                                 # truncated file; use what we have
            raw = raw[: len(raw) - (len(raw) % frame_bytes)]
            remaining -= len(raw)
            yield _decode(raw, info.bits)


def read_baseband(path: str | Path) -> tuple[np.ndarray, float, float | None]:
    """Whole file at once: (complex samples, sample rate, centre or None).

    For tests and short captures only. Refuses anything over MAX_EAGER_BYTES, because an
    hour of 250 kSPS IQ is 14.4 GB in complex128 and a convenience function is exactly the
    sort of thing that ends up called on it by accident.
    """
    info = open_baseband(path)
    if info.data_bytes > MAX_EAGER_BYTES:
        raise ValueError(
            f"{Path(path).name}: {info.data_bytes/1e9:.2f} GB is too large to read at once; "
            f"use iter_baseband()")
    blocks = list(iter_baseband(path))
    samples = np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.complex128)
    return samples, info.rate, info.centre_hz


def synth_nfm(audio: np.ndarray, audio_rate: float, iq_rate: float,
              deviation_hz: float = 3000.0, offset_hz: float = 0.0,
              noise_db: float | None = None) -> np.ndarray:
    """Narrowband FM at `offset_hz` from DC, for tests.

    Constant envelope by construction: FM carries its information in phase alone, so a
    synthesiser that let amplitude vary would give the discriminator a second channel to
    cheat on and the demodulator tests would pass for the wrong reason.
    """
    audio = np.asarray(audio, dtype=np.float64)
    n_out = int(len(audio) * iq_rate / audio_rate) if len(audio) else 0
    if n_out <= 0:
        return np.zeros(0, dtype=np.complex128)

    # Resample the modulating audio up to the IQ rate by linear interpolation. Good enough:
    # the test signals are smooth tones well below the IQ Nyquist.
    src = np.linspace(0.0, len(audio) - 1, n_out) if len(audio) > 1 else np.zeros(n_out)
    up = np.interp(src, np.arange(len(audio)), audio)

    phase = 2 * np.pi * deviation_hz * np.cumsum(up) / iq_rate
    t = np.arange(n_out) / iq_rate
    iq = np.exp(1j * (phase + 2 * np.pi * offset_hz * t))

    if noise_db is not None:
        amp = 10 ** (noise_db / 20.0)
        rng = np.random.default_rng(0xC0FFEE)   # fixed: a flaky DSP test is worthless
        iq = iq + amp * (rng.normal(size=n_out) + 1j * rng.normal(size=n_out)) / np.sqrt(2)
    return iq
