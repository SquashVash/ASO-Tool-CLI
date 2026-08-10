"""Background jobs, held in memory.

Deliberately not a table. A job IS an asyncio task in this process — kill the
process and the run dies with it — so a persisted `status='running'` row would
outlive the thing it describes and lie to whoever read it next. A restart
loses the history, and the mitigation is that the history was never the record:
the snapshots in `aso.db` are, plus journald.

One slot per kind. Two refreshes writing snapshots for an overlapping keyword
set is wrong; a refresh alongside an ASA pull is not, because they write
different tables and share one token bucket.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from ..db import utcnow

logger = logging.getLogger(__name__)

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    kind: str
    status: str = RUNNING
    started_at: str = ""
    finished_at: str | None = None
    params: dict = field(default_factory=dict)
    done: int = 0
    total: int | None = None
    current: str | None = None
    result: dict | None = None
    error: str | None = None


class JobConflict(RuntimeError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"A {kind} job is already running")


JobBody = Callable[[Job], Awaitable[dict]]


class JobRegistry:
    def __init__(self, history: int = 50) -> None:
        self._history = history
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def running(self, kind: str) -> Job | None:
        for job in self._jobs.values():
            if job.kind == kind and job.status == RUNNING:
                return job
        return None

    async def start(self, kind: str, run: JobBody, *, params: dict | None = None) -> Job:
        if self.running(kind) is not None:
            raise JobConflict(kind)
        job = Job(id=uuid4().hex, kind=kind, started_at=utcnow(), params=params or {})
        self._jobs[job.id] = job
        self._tasks[job.id] = asyncio.create_task(self._run(job, run))
        # Give the task a real turn before returning. A task cancelled before
        # it has ever run skips its body entirely — including the `finally`
        # that stamps `finished_at` — so a caller that starts a job and
        # immediately cancels or shuts down would see it wedged at "running"
        # forever.
        await asyncio.sleep(0)
        self._trim()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda job: job.started_at, reverse=True)

    async def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None:
            return False
        task.cancel()
        return True

    async def wait(self, job_id: str) -> None:
        """Block until a job settles. For tests and for shutdown."""
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _run(self, job: Job, run: JobBody) -> None:
        try:
            job.result = await run(job)
            job.status = SUCCEEDED
        except asyncio.CancelledError:
            job.status = CANCELLED
            logger.info("job %s (%s) cancelled after %s items", job.id, job.kind, job.done)
            raise
        except Exception as exc:  # a failed run must not take the server down
            job.status = FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            logger.exception("job %s (%s) failed", job.id, job.kind)
        finally:
            job.finished_at = utcnow()
            self._tasks.pop(job.id, None)
            # `start()` also trims, but only on the next call — a job that
            # finishes without a subsequent `start()` would otherwise sit
            # past the history limit forever.
            self._trim()

    def _trim(self) -> None:
        finished = [job for job in self.list() if job.status != RUNNING]
        for job in finished[self._history :]:
            self._jobs.pop(job.id, None)
