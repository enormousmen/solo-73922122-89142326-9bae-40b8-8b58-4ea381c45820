"""Bounded, non-blocking collection of a child process's stderr.

Why this exists
---------------
A misconfigured ffmpeg can write megabytes of diagnostics before exiting.
Redirecting stderr to a file lets it grow without bound on disk, while
``subprocess.run(..., stderr=PIPE)`` buffers everything in memory. Both grow
with the amount of output. And *not* draining a stderr pipe at all eventually
fills the OS pipe (typically 64 KiB), at which point the child blocks forever
on a write — wedging a transcode slot.

``BoundedStderrCollector`` solves all three problems at once:

* stderr is a real pipe, drained continuously by a dedicated daemon thread, so
  the child can never block on pipe capacity, no matter how much it writes;
* only the last ``max_bytes`` bytes are ever retained, so memory use is fixed
  regardless of total output volume;
* the thread finishes on its own when the process closes the pipe at exit, so
  the final log tail (written immediately before exit) is always available.
"""

from __future__ import annotations

import subprocess
import threading
from typing import BinaryIO


class BoundedStderrCollector:
    """Drain ``proc.stderr`` in a background thread, keeping only the tail.

    The retained buffer never exceeds ``max_bytes``; ``truncated`` reports
    whether bytes were discarded from the front.
    """

    def __init__(self, proc: subprocess.Popen, max_bytes: int) -> None:
        if proc.stderr is None:
            raise ValueError("process must be opened with stderr=subprocess.PIPE")
        self._stream: BinaryIO = proc.stderr
        self._max_bytes = max(1, int(max_bytes))
        self._lock = threading.Lock()
        self._tail = bytearray()
        self._total = 0
        self.truncated = False
        self._thread = threading.Thread(
            target=self._drain, name="ffmpeg-stderr-drain", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _drain(self) -> None:
        # Read in modest chunks so a burst of output is reclaimed promptly;
        # the stream is a pipe, so this never reads more than what is buffered.
        while True:
            try:
                chunk = self._stream.read(4096)
            except (OSError, ValueError):
                # The pipe was closed out from under us (e.g. shutdown).
                break
            if not chunk:
                break
            with self._lock:
                self._total += len(chunk)
                self._tail.extend(chunk)
                overflow = len(self._tail) - self._max_bytes
                if overflow > 0:
                    # Dropping the prefix keeps this a true "last N bytes"
                    # ring buffer at fixed, output-independent memory cost.
                    del self._tail[:overflow]
                    self.truncated = True
        try:
            self._stream.close()
        except OSError:
            pass

    def finish(self, timeout: float | None = None) -> None:
        """Wait until the process has closed stderr and the tail is complete.

        Called only on the failure path: success/finalisation decisions must
        not wait for log drainage. The drain thread is a daemon and normally
        reaches EOF on its own within a scheduler tick anyway.
        """

        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total

    def tail_bytes(self) -> bytes:
        with self._lock:
            return bytes(self._tail)

    def tail_text(self) -> tuple[str, bool]:
        """Return ``(decoded tail, truncated)`` with lenient UTF-8 handling."""

        with self._lock:
            data = bytes(self._tail)
            truncated = self.truncated
        return data.decode("utf-8", "replace").strip(), truncated
