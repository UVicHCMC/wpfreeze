from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from wpfreeze.runlock import RunLock, RunLockHeld


def _dead_pid() -> int:
    """A PID guaranteed not to be running: spawn a subprocess, wait for
    it to exit and be reaped, then hand back its (now-free) PID."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_acquire_and_release_round_trip(tmp_path: Path):
    lock = RunLock(tmp_path)
    lock.acquire()
    assert lock.path.exists()
    assert int(lock.path.read_text()) == os.getpid()
    lock.release()
    assert not lock.path.exists()


def test_context_manager_releases_on_exception(tmp_path: Path):
    with pytest.raises(RuntimeError):
        with RunLock(tmp_path):
            raise RuntimeError("boom")
    assert not (tmp_path / ".wpfreeze.lock").exists()


def test_second_acquire_refused_while_first_pid_alive(tmp_path: Path):
    first = RunLock(tmp_path)
    first.acquire()
    try:
        second = RunLock(tmp_path)
        with pytest.raises(RunLockHeld):
            second.acquire()
    finally:
        first.release()


def test_stale_lock_from_dead_pid_is_cleared_and_reacquired(tmp_path: Path):
    lock_path = tmp_path / ".wpfreeze.lock"
    lock_path.write_text(str(_dead_pid()))

    lock = RunLock(tmp_path)
    lock.acquire()  # must not raise
    assert int(lock_path.read_text()) == os.getpid()
    lock.release()


def test_garbage_lock_contents_treated_as_stale(tmp_path: Path):
    lock_path = tmp_path / ".wpfreeze.lock"
    lock_path.write_text("not-a-pid")

    lock = RunLock(tmp_path)
    lock.acquire()  # must not raise
    lock.release()
