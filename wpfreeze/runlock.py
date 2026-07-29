"""Advisory cross-process lock guarding writes to one output_dir's
manifest.json.

Manifest.save is atomic per write (see manifest.py) but that is a
single-process guarantee -- there is no exclusion between two separate
`wpfreeze` processes pointed at the same output_dir, so the second one to
save silently discards whatever the first had written. This has always
been a hazard, but `rescan` makes it materially more likely: running it
"just to see" while an acquire is already in flight against the same
directory is exactly the scenario that costs hours of crawling.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".wpfreeze.lock"


class RunLockHeld(Exception):
    """Another live wpfreeze process already holds this output_dir's lock."""


def _pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


class RunLock:
    """Context manager acquiring output_dir/.wpfreeze.lock for the
    duration of a manifest-writing command (`acquire`, `rescan --apply`).

    Not a substitute for a real filesystem lock -- it catches the routine
    case of a second invocation against the same output_dir, not every
    possible race. A lock file naming a PID that is no longer running is
    assumed stale and cleared automatically.
    """

    def __init__(self, output_dir: Path) -> None:
        self.path = Path(output_dir) / LOCK_FILENAME

    def _read_pid(self) -> int | None:
        try:
            return int(self.path.read_text().strip())
        except (ValueError, OSError):
            return None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            held_pid = self._read_pid()
            if held_pid is not None and _pid_alive(held_pid):
                raise RunLockHeld(
                    f"{self.path} is held by process {held_pid}, which is still "
                    "running. Only one wpfreeze command may write this output "
                    "directory's manifest at a time."
                )
            logger.info(
                "clearing stale lock at %s (pid %s no longer running)", self.path, held_pid
            )
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RunLockHeld(f"{self.path} was just claimed by another process") from exc
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))

    def release(self) -> None:
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
