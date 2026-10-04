"""Keeping the Mac awake while paper (and later live) trading runs."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)


class KeepAwake:
    """Runs macOS `caffeinate -i` (no idle sleep) for as long as this app runs and it is needed."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.platform == "darwin" and shutil.which("caffeinate") is not None
        self.process: subprocess.Popen | None = None

    @property
    def active(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def update(self, needed: bool) -> None:
        if needed and self.enabled and not self.active:
            # -w: stop automatically when this app's process ends, even after a crash.
            self.process = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            log.info("Keeping the Mac awake (caffeinate)")
        elif not needed and self.active:
            self.stop()

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            self.process = None
            log.info("Mac may sleep again")
