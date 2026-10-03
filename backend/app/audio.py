"""Audio inspection and silence-aware chunking with ffmpeg/ffprobe.

Gnani's synchronous endpoint accepts at most 60 s per request and works best at
<= 30 s, so long recordings are split into ~25 s pieces. Cuts are placed in the
middle of detected pauses so words are not sliced in half; only if there is no
pause in the allowed window do we fall back to a hard cut.
"""

from __future__ import annotations

import json
import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from .errors import InvalidAudioError

SAMPLE_RATE = 16000


@dataclass
class ProbeResult:
    duration_s: float
    codec: str | None
    channels: int | None
    sample_rate: int | None


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe(path: Path) -> ProbeResult:
    try:
        r = _run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            timeout=120,
        )
    except subprocess.TimeoutExpired as e:
        raise InvalidAudioError("Timed out while reading the file; it may be corrupted.") from e
    if r.returncode != 0:
        raise InvalidAudioError(
            "This file could not be read as audio. It may be corrupted or in an unsupported format."
        )
    info = json.loads(r.stdout or "{}")
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise InvalidAudioError("The file contains no audio track.")
    dur = audio.get("duration") or (info.get("format") or {}).get("duration")
    return ProbeResult(
        duration_s=float(dur) if dur not in (None, "N/A") else 0.0,
        codec=audio.get("codec_name"),
        channels=audio.get("channels"),
        sample_rate=int(audio["sample_rate"]) if audio.get("sample_rate") else None,
    )


def normalize(src: Path, dest: Path) -> float:
    """Decode anything ffmpeg understands into 16 kHz mono 16-bit PCM WAV.

    Fully decoding the file is also our corruption check: a truncated or
    damaged file fails here rather than half-way through transcription.
    Returns the true decoded duration in seconds.
    """
    try:
        r = _run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1",
             "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", "-f", "wav", str(dest)],
            timeout=1800,
        )
    except subprocess.TimeoutExpired as e:
        raise InvalidAudioError("Decoding the audio took too long; the file may be damaged.") from e
    if r.returncode != 0 or not dest.exists():
        tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
        raise InvalidAudioError(f"The audio could not be decoded ({tail[0][:160] or 'ffmpeg error'}).")
    with wave.open(str(dest), "rb") as w:
        return w.getnframes() / w.getframerate()


_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def detect_silences(wav: Path, noise_db: float = -32, min_dur: float = 0.3) -> list[tuple[float, float]]:
    r = _run(
        ["ffmpeg", "-nostdin", "-v", "info", "-i", str(wav), "-af",
         f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
        timeout=600,
    )
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in (r.stderr or "").splitlines():
        if m := _SIL_START.search(line):
            start = max(0.0, float(m.group(1)))
        elif (m := _SIL_END.search(line)) and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    return silences


def plan_chunks(
    duration: float,
    silences: list[tuple[float, float]],
    target: float = 25.0,
    min_len: float = 12.0,
    max_len: float = 29.0,
) -> list[tuple[float, float]]:
    """Return [(start, end), ...] covering [0, duration] with every piece <= max_len.

    Within each window [start+min_len, start+max_len] we cut at the pause whose
    midpoint is closest to start+target, preferring longer pauses.
    """
    if duration <= max_len:
        return [(0.0, round(duration, 3))]
    candidates = sorted(((a + b) / 2, b - a) for a, b in silences)
    chunks: list[tuple[float, float]] = []
    start = 0.0
    while duration - start > max_len:
        lo, hi = start + min_len, start + max_len
        window = [(t, d) for t, d in candidates if lo <= t <= hi]
        if window:
            # Score: distance from target, discounted for longer pauses.
            cut = min(window, key=lambda c: abs(c[0] - (start + target)) - 2.0 * min(c[1], 1.5))[0]
        else:
            cut = start + target  # no pause: hard cut
        chunks.append((round(start, 3), round(cut, 3)))
        start = cut
    chunks.append((round(start, 3), round(duration, 3)))
    # Avoid a tiny trailing piece by merging/splitting the last two evenly.
    if len(chunks) >= 2 and chunks[-1][1] - chunks[-1][0] < 3.0:
        (a, _), (_, z) = chunks[-2], chunks[-1]
        if z - a <= max_len:
            chunks[-2:] = [(a, z)]
        else:
            mid = round((a + z) / 2, 3)
            chunks[-2:] = [(a, mid), (mid, z)]
    return chunks


def slice_wav(wav: Path, start: float, end: float) -> bytes:
    """Cut [start, end) out of a PCM WAV exactly, returning a standalone WAV file."""
    import io

    with wave.open(str(wav), "rb") as w:
        rate = w.getframerate()
        first = int(start * rate)
        count = max(0, int(end * rate) - first)
        w.setpos(min(first, w.getnframes()))
        frames = w.readframes(count)
        params = w.getparams()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setparams(params)
        out.writeframes(frames)
    return buf.getvalue()
