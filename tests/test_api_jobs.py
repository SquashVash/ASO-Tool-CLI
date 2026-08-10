"""The in-memory job registry.

Jobs are not persisted. A job IS an asyncio task in this process: if the
process dies the run dies with it, and a stored status='running' row would
outlive the thing it describes and lie to the next caller.
"""

from __future__ import annotations

import asyncio

import pytest

from aso.api.jobs import JobConflict, JobRegistry


async def test_a_job_runs_and_records_its_result():
    registry = JobRegistry()

    async def run(job):
        job.total = 2
        job.done = 2
        return {"succeeded": 2}

    job = await registry.start("refresh", run)
    await registry.wait(job.id)

    assert registry.get(job.id).status == "succeeded"
    assert registry.get(job.id).result == {"succeeded": 2}
    assert registry.get(job.id).finished_at is not None


async def test_a_second_job_of_the_same_kind_is_refused():
    """Two refreshes writing snapshots for an overlapping set is wrong."""
    registry = JobRegistry()
    gate = asyncio.Event()

    async def blocking(job):
        await gate.wait()
        return {}

    await registry.start("refresh", blocking)
    with pytest.raises(JobConflict):
        await registry.start("refresh", blocking)

    gate.set()


async def test_different_kinds_may_run_concurrently():
    """A refresh and an ASA pull write different tables, and the shared token
    bucket means they cannot together overrun the rate limit."""
    registry = JobRegistry()
    gate = asyncio.Event()

    async def blocking(job):
        await gate.wait()
        return {}

    await registry.start("refresh", blocking)
    await registry.start("asa_pull", blocking)
    gate.set()


async def test_a_failing_job_records_the_error_and_does_not_raise():
    registry = JobRegistry()

    async def boom(job):
        raise RuntimeError("apple said no")

    job = await registry.start("refresh", boom)
    await registry.wait(job.id)

    assert registry.get(job.id).status == "failed"
    assert "apple said no" in registry.get(job.id).error


async def test_cancel_marks_the_job_cancelled_and_keeps_partial_progress():
    registry = JobRegistry()
    started = asyncio.Event()

    async def slow(job):
        job.done = 3
        started.set()
        await asyncio.sleep(60)
        return {}

    job = await registry.start("refresh", slow)
    await started.wait()
    assert await registry.cancel(job.id) is True
    await registry.wait(job.id)

    assert registry.get(job.id).status == "cancelled"
    assert registry.get(job.id).done == 3


async def test_cancelling_an_unknown_job_returns_false():
    assert await JobRegistry().cancel("nope") is False


async def test_history_is_bounded_but_never_evicts_a_running_job():
    registry = JobRegistry(history=3)
    gate = asyncio.Event()

    async def blocking(job):
        await gate.wait()
        return {}

    long_running = await registry.start("refresh", blocking)
    for _ in range(5):
        finished = await registry.start("rescore", lambda job: _done())
        await registry.wait(finished.id)

    assert registry.get(long_running.id) is not None
    assert len(registry.list()) <= 4  # 3 finished + the running one
    gate.set()


async def _done():
    return {}


async def test_shutdown_cancels_running_jobs():
    """systemctl restart must not orphan a three-hour refresh."""
    registry = JobRegistry()

    async def slow(job):
        await asyncio.sleep(60)
        return {}

    job = await registry.start("refresh", slow)
    await registry.shutdown()

    assert registry.get(job.id).status == "cancelled"
