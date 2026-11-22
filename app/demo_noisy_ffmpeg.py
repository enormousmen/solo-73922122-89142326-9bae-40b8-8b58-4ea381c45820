#!/usr/bin/env python3
"""Maintenance demo wrapper: ffmpeg that floods stderr then fails.

The shipped app points ``FFMPEG_BINARY`` at the real ffmpeg. To let a
maintainer reproduce "a transcode writes massive logs and then fails" from the
web UI, the docker-compose ``noisy-failure`` profile overrides
``FFMPEG_BINARY`` with this script instead.

The wrapper stays transparent for everything except real transcode attempts:

* fixture generation, probing (``-f null -``) and concat are forwarded
  untouched, so the app still starts, generates clips and composes cuts;
* a transcode whose output name starts with the configured
  ``NOISY_FAIL_CLIP`` (default ``B``) writes ``NOISY_FAIL_BYTES`` of stderr
  (well over any pipe capacity) and exits non-zero, like a real ffmpeg that
  logs a huge diagnostic dump before failing;
* every other transcode is forwarded to the real ffmpeg.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_transcode(argv: list[str]) -> bool:
    # Probe calls end in "-", concat uses "-f concat"; only encode attempts
    # carry "-nostdin" and an mp4 output path.
    return "-nostdin" in argv and argv and argv[-1].endswith(".mp4")


def output_segment(argv: list[str]) -> str:
    # Scheduler targets are named like "B_<attempt-id>.mp4".
    stem = Path(argv[-1]).stem
    return stem.split("_", 1)[0] if "_" in stem else ""


def run() -> int:  # pragma: no cover - exercised via docker compose only
    argv = sys.argv[1:]
    target_clip = os.environ.get("NOISY_FAIL_CLIP", "B").strip().upper()
    noisy_bytes = int(os.environ.get("NOISY_FAIL_BYTES", str(2 * 1024 * 1024)))

    if is_transcode(argv) and output_segment(argv) == target_clip:
        line = (
            "noisy-ffmpeg-demo: repeating diagnostic line "
            "0123456789abcdef0123456789abcdef\n"
        ).encode("ascii")
        remaining = noisy_bytes
        sys.stderr.buffer.write(
            b"noisy-ffmpeg-demo: injected failure for segment "
            + target_clip.encode()
            + b"\n"
        )
        while remaining > 0:
            part = line if remaining >= len(line) else line[:remaining]
            try:
                sys.stderr.buffer.write(part)
                sys.stderr.buffer.flush()
            except BrokenPipeError:
                break
            remaining -= len(part)
        sys.stderr.buffer.write(
            b"noisy-ffmpeg-demo: FINAL-LINE-BEFORE-EXIT simulated encoder fault\n"
        )
        sys.stderr.buffer.flush()
        return 1

    # Transparent pass-through for fixtures, probes, concat and other clips.
    real_ffmpeg = os.environ.get("REAL_FFMPEG_BINARY", "ffmpeg")
    os.execv(real_ffmpeg, [real_ffmpeg, *argv])
    return 127  # execv only returns on failure


if __name__ == "__main__":
    sys.exit(run())
