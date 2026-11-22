"""Bounded stderr collection: fixed memory, no pipe deadlock, correct tail."""

from __future__ import annotations

import subprocess
import sys
import time

from app.stderr_capture import BoundedStderrCollector


def _noisy_child(total_bytes: int, *, hold: float = 0.0) -> list[str]:
    # Write exactly total_bytes in tight loops, then optionally linger with the
    # pipe still open, to prove the collector drains concurrently.
    code = (
        "import sys,time\n"
        f"n={total_bytes}\n"
        "line=b'x'*1023+b'\\n'\n"
        "out=sys.stderr.buffer\n"
        "while n>0:\n"
        "    part=line if n>=len(line) else line[:n]\n"
        "    out.write(part); out.flush(); n-=len(part)\n"
        f"time.sleep({hold})\n"
    )
    return [sys.executable, "-c", code]


def test_collector_keeps_only_last_n_bytes_and_marks_truncation():
    # Each line carries its own index, so we can tell a real tail from a prefix.
    code = (
        "import sys\n"
        "out=sys.stderr.buffer\n"
        "for i in range(400):\n"
        "    out.write(f'line-{i:06d}' .encode()+b'\\n')\n"
        "    out.flush()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    collector = BoundedStderrCollector(proc, max_bytes=4096)
    collector.start()
    assert proc.wait(timeout=15) == 0
    collector.finish(timeout=5)

    tail = collector.tail_bytes()
    assert len(tail) == 4096
    assert collector.truncated is True
    assert collector.total_bytes == 400 * 12
    text = tail.decode("ascii")
    # The final line ever written is in the retained tail; early lines are gone.
    assert "line-000399" in text
    assert "line-000000" not in text
    assert text.strip().splitlines()[-1] == "line-000399"


def test_collector_drains_concurrently_so_child_never_blocks_on_pipe():
    # Many megabytes through a ~64 KiB pipe while the child stays alive: without
    # continuous draining this process could not finish within any timeout.
    proc = subprocess.Popen(
        _noisy_child(4 * 1024 * 1024, hold=0.3),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    collector = BoundedStderrCollector(proc, max_bytes=4096)
    start = time.monotonic()
    collector.start()
    assert proc.wait(timeout=15) == 0
    collector.finish(timeout=5)
    assert time.monotonic() - start < 15
    assert len(collector.tail_bytes()) <= 4096
    assert collector.total_bytes >= 4 * 1024 * 1024


def test_collector_below_limit_is_not_marked_truncated():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys;sys.stderr.write('short log\\n')"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    collector = BoundedStderrCollector(proc, max_bytes=4096)
    collector.start()
    assert proc.wait(timeout=10) == 0
    collector.finish(timeout=5)
    text, truncated = collector.tail_text()
    assert truncated is False
    assert text == "short log"


def test_retained_memory_does_not_grow_with_output_volume():
    # 256 KiB and 8 MiB of output must leave the same bounded footprint.
    sizes = (256 * 1024, 8 * 1024 * 1024)
    retained: list[int] = []
    for size in sizes:
        proc = subprocess.Popen(
            _noisy_child(size),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        collector = BoundedStderrCollector(proc, max_bytes=4096)
        collector.start()
        assert proc.wait(timeout=30) == 0
        collector.finish(timeout=5)
        retained.append(len(collector.tail_bytes()))
    assert retained == [4096, 4096]
