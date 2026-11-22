"""Scheduler behaviour around ffmpeg processes that emit huge stderr streams."""

from __future__ import annotations

import threading
import time

from app import scheduler as scheduler_module


def _segment(batch: dict, name: str) -> dict:
    return next(s for s in batch["segments"] if s["name"] == name)


def _attempts(batch: dict, name: str, kind: str | None = None) -> list[dict]:
    items = _segment(batch, name)["attempts"]
    return [a for a in items if kind is None or a["kind"] == kind]


def test_huge_stderr_does_not_deadlock_and_batch_still_succeeds(started):
    # ~2 MiB per transcode is far beyond the 64 KiB pipe capacity: a blocked
    # pipe would keep every attempt "running" forever.
    started.write_fake_config({"sleep": 0.05, "stderr_bytes": 2 * 1024 * 1024})
    manager = started.manager
    batch = manager.create_batch("A")

    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "ready"
        else None,
        timeout=20,
    )
    assert final["status"] == "ready"
    assert all(a["status"] == "succeeded" for a in final["attempts"])

    # Memory stayed bounded: each collector retains at most the configured cap.
    live = started.manager
    with live._lock:
        for stored_batch in live._batches.values():
            for attempt in stored_batch.attempts():
                collector = attempt.stderr_collector
                assert collector is not None
                assert len(collector.tail_bytes()) <= started.settings.stderr_tail_bytes
                assert collector.total_bytes >= 2 * 1024 * 1024


def test_failed_attempt_waits_for_stderr_and_shows_pre_exit_tail(started):
    started.write_fake_config(
        {
            "segments": {"B": {"normal": {"sleep": 0.05, "exit": 7}}},
            "sleep": 0.05,
            "stderr_bytes": 128 * 1024,
        }
    )
    manager = started.manager
    batch = manager.create_batch("A")

    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "failed"
        else None,
        timeout=15,
    )
    failed = _attempts(final, "B")[0]
    assert failed["status"] == "failed"
    assert failed["exit_code"] == 7
    # The line written immediately before exit must be present.
    assert "FINAL-LINE-BEFORE-EXIT" in failed["error"]
    assert "ffmpeg 退出码 7" in failed["error"]
    # Truncation is announced because 128 KiB > 4 KiB retained tail.
    assert "日志已截断" in failed["error"]
    assert "4096" in failed["error"]


def test_short_failure_log_has_no_truncation_note(started):
    started.write_fake_config(
        {"segments": {"C": {"normal": {"sleep": 0.02, "exit": 2}}}}
    )
    manager = started.manager
    batch = manager.create_batch("A")
    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "failed"
        else None,
        timeout=10,
    )
    error = _attempts(final, "C")[0]["error"]
    assert "FINAL-LINE-BEFORE-EXIT" in error
    assert "日志已截断" not in error


def test_success_path_does_not_wait_for_stderr_drain(started, monkeypatch):
    # If finalisation joined the drain thread on success, this spy would be hit
    # on every attempt. Selection must be decided without it.
    joins: list[str] = []
    original = scheduler_module.BoundedStderrCollector.finish

    def spy_finish(self, timeout=None):  # noqa: ANN001
        joins.append(threading.current_thread().name)
        return original(self, timeout)

    monkeypatch.setattr(scheduler_module.BoundedStderrCollector, "finish", spy_finish)
    started.write_fake_config(
        {"sleep": 0.05, "stderr_bytes": 256 * 1024, "stderr_before_sleep": True}
    )
    manager = started.manager
    batch = manager.create_batch("A")
    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "ready"
        else None,
        timeout=15,
    )
    assert final["status"] == "ready"
    assert joins == []


def test_slot_is_returned_immediately_when_process_exits(started, monkeypatch):
    # Slow the drain thread down after the process has exited and prove the
    # slot counter drops before the (failure-path) drain wait completes.
    original_finish = scheduler_module.BoundedStderrCollector.finish
    released_during_wait: list[bool] = []
    manager_lock = None

    def slow_finish(self, timeout=None):  # noqa: ANN001
        # Capture the slot state while a drain is supposedly still pending.
        if manager_lock is not None:
            with manager_lock:
                released_during_wait.append(started.manager._slots_used)
        time.sleep(0.2)
        return original_finish(self, timeout)

    monkeypatch.setattr(scheduler_module.BoundedStderrCollector, "finish", slow_finish)
    started.write_fake_config(
        {
            "segments": {"B": {"normal": {"sleep": 0.05, "exit": 5}}},
            "sleep": 0.05,
            "stderr_bytes": 64 * 1024,
        }
    )
    manager_lock = started.manager._lock
    manager = started.manager
    batch = manager.create_batch("A")
    started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "failed"
        else None,
        timeout=15,
    )
    assert released_during_wait, "failure path should have awaited the drain"
    # By the time we wait on drainage, the exited process's slot is already back.
    assert min(released_during_wait) <= 1
    assert started.manager.slots_used == 0


def test_first_valid_artifact_wins_even_when_its_stderr_drains_late(started, monkeypatch):
    # The first run produces its artifact and exits first, but a monkeypatched
    # drain makes its stderr collection complete only much later. The backup is
    # launched (slow threshold) and is still running when the first run exits,
    # so selection must go to the first run immediately on artifact validity,
    # without waiting for its drain — and the backup must be cancelled.
    original_finish = scheduler_module.BoundedStderrCollector.finish

    def slow_finish(self, timeout=None):  # noqa: ANN001
        time.sleep(0.8)
        return original_finish(self, timeout)

    monkeypatch.setattr(scheduler_module.BoundedStderrCollector, "finish", slow_finish)
    started.write_fake_config(
        {
            "segments": {
                "A": {
                    # The slow first run clearly clears the 0.35s backup
                    # threshold before exiting at 0.9s; the backup itself
                    # sleeps long enough to still be running when that happens.
                    "slow": {
                        "sleep": 0.9,
                        "stderr_bytes": 512 * 1024,
                        "stderr_before_sleep": True,
                    },
                    "normal": {"sleep": 3.0},
                }
            },
            "sleep": 0.05,
        }
    )
    manager = started.manager
    batch = manager.create_batch("A")

    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "ready"
        else None,
        timeout=15,
    )
    assert final["status"] == "ready"
    segment = _segment(final, "A")
    first = _attempts(final, "A", "first")[0]
    backups = _attempts(final, "A", "backup")
    assert segment["selected_attempt_id"] == first["id"]
    assert first["status"] == "succeeded"
    assert backups, "the slow threshold should still have launched a backup"

    # The backup never wins: by the time it settles it has been cancelled (or,
    # if it exits 0 just before the signal lands, recorded as superseded).
    def backup_settled():
        view = started.batch(batch["id"])
        items = _attempts(view, "A", "backup")
        if items and items[0]["status"] in ("cancelled", "superseded", "skipped", "failed"):
            return items[0]
        return None

    settled_backup = started.wait_for(backup_settled, timeout=10)
    assert settled_backup["status"] in ("cancelled", "superseded", "skipped", "failed")
    assert _segment(started.batch(batch["id"]), "A")["selected_attempt_id"] == first["id"]
    # The first run's collector still holds its bounded tail, drained late.
    with manager._lock:
        stored_first = next(
            a
            for b in manager._batches.values()
            for a in b.attempts()
            if a.id == first["id"]
        )
        assert stored_first.stderr_collector is not None
        assert len(stored_first.stderr_collector.tail_bytes()) <= 4096
        assert stored_first.stderr_collector.total_bytes >= 512 * 1024


def test_other_batches_keep_dispatching_while_a_noisy_process_runs(started):
    # Batch 1 gets a long, extremely noisy attempt occupying one slot;
    # batch 2 must still flow through the other slot and finish normally.
    started.write_fake_config(
        {
            "segments": {
                "A": {"slow": {"sleep": 4.0, "stderr_bytes": 1024 * 1024}},
            },
            "sleep": 0.05,
            "stderr_bytes": 0,
        }
    )
    manager = started.manager
    first = manager.create_batch("A")
    second = manager.create_batch("C")

    final_second = started.wait_for(
        lambda: started.batch(second["id"])
        if started.batch(second["id"])["status"] == "ready"
        else None,
        timeout=20,
    )
    assert final_second["status"] == "ready"
    # Meanwhile the first batch is still progressing, not wedged.
    first_view = started.batch(first["id"])
    assert first_view["status"] in ("running", "composing", "ready")
    # Web-style state queries keep answering while noise is being produced.
    for _ in range(5):
        assert manager.get_batch(first["id"]) is not None
        assert manager.list_batches()
        time.sleep(0.02)

    started.wait_for(
        lambda: started.batch(first["id"])
        if started.batch(first["id"])["status"] in ("ready", "failed")
        else None,
        timeout=20,
    )
    assert started.batch(first["id"])["status"] == "ready"
