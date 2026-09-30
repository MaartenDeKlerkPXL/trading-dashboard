"""Background download jobs, so the dashboard can show progress."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field

from .store import CandleStore

log = logging.getLogger(__name__)


@dataclass
class SyncJob:
    id: str
    symbol: str
    level: str
    start: int
    end: int
    status: str = "running"   # running | done | error
    done: int = 0
    total: int = 0
    downloaded: int = 0
    failed: int = 0
    message: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class JobManager:
    def __init__(self, store: CandleStore):
        self.store = store
        self.jobs: dict[str, SyncJob] = {}
        self._tasks: set[asyncio.Task] = set()

    def start_sync(self, symbol: str, level: str, start: int, end: int) -> SyncJob:
        # Reuse a running job for the same request instead of downloading twice.
        for job in self.jobs.values():
            if job.status == "running" and (job.symbol, job.level, job.start, job.end) == (symbol, level, start, end):
                return job
        job = SyncJob(id=uuid.uuid4().hex[:12], symbol=symbol, level=level, start=start, end=end)
        self.jobs[job.id] = job
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._prune()
        return job

    async def _run(self, job: SyncJob) -> None:
        def progress(done: int, total: int) -> None:
            job.done, job.total = done, total

        try:
            result = await self.store.sync(job.symbol, job.level, job.start, job.end, progress)
        except Exception as exc:  # report every failure to the UI, never lose it silently
            log.exception("Sync job %s failed", job.id)
            job.status, job.message = "error", f"Onverwachte fout: {exc}"
            return
        job.downloaded, job.failed = result.downloaded, result.failed
        if result.failed and result.downloaded == 0:
            job.status = "error"
            job.message = result.errors[0] if result.errors else "Downloaden mislukt"
        else:
            job.status = "done"
            if result.failed:
                job.message = f"{result.failed} van {result.total} blokken niet opgehaald; probeer later opnieuw."
        log.info(
            "Sync %s %s: %d downloaded, %d cached, %d failed",
            job.symbol, job.level, result.downloaded, result.skipped, result.failed,
        )

    def _prune(self, keep: int = 50) -> None:
        finished = [j for j in self.jobs.values() if j.status != "running"]
        for job in sorted(finished, key=lambda j: j.created_at)[:-keep]:
            self.jobs.pop(job.id, None)
