"""Long-running background tasks (optimizations) with progress and cancellation."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


class Cancelled(Exception):
    pass


@dataclass
class Task:
    id: str
    kind: str
    status: str = "running"      # running | done | error | cancelled
    done: int = 0
    total: int = 0
    message: str = ""
    result: dict | None = None
    created_at: float = field(default_factory=time.time)
    cancel_requested: bool = False

    def progress(self, done: int, total: int) -> None:
        """Called from the worker thread. Raises Cancelled when the user stopped the task."""
        self.done, self.total = done, total
        if self.cancel_requested:
            raise Cancelled()

    def to_dict(self, with_result: bool = True) -> dict:
        data = {k: getattr(self, k) for k in ("id", "kind", "status", "done", "total", "message", "created_at")}
        if with_result:
            data["result"] = self.result
        return data


class TaskManager:
    def __init__(self, keep: int = 20):
        self.tasks: dict[str, Task] = {}
        self.keep = keep
        self._handles: set[asyncio.Task] = set()

    def start(self, kind: str, work: Callable[[Task], dict]) -> Task:
        """Run `work(task)` in a worker thread; it reports progress through task.progress()."""
        task = Task(id=uuid.uuid4().hex[:12], kind=kind)
        self.tasks[task.id] = task

        async def runner() -> None:
            try:
                task.result = await asyncio.to_thread(work, task)
                task.status = "done"
            except Cancelled:
                task.status, task.message = "cancelled", "Gestopt."
            except ValueError as exc:
                task.status, task.message = "error", str(exc)
            except Exception as exc:  # report every failure to the UI, never lose it silently
                log.exception("Task %s failed", task.id)
                task.status, task.message = "error", f"Onverwachte fout: {exc}"

        handle = asyncio.create_task(runner())
        self._handles.add(handle)
        handle.add_done_callback(self._handles.discard)
        self._prune()
        return task

    def running(self, kind: str) -> Task | None:
        return next((t for t in self.tasks.values() if t.kind == kind and t.status == "running"), None)

    def _prune(self) -> None:
        finished = sorted((t for t in self.tasks.values() if t.status != "running"), key=lambda t: t.created_at)
        for task in finished[:-self.keep]:
            self.tasks.pop(task.id, None)
