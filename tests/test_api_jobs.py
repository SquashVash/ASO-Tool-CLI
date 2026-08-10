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

    job = await registry.start("refresh", blocking)
    with pytest.raises(JobConflict):
        await registry.start("refresh", blocking)

    gate.set()
    await registry.wait(job.id)


async def test_different_kinds_may_run_concurrently():
    """A refresh and an ASA pull write different tables, and the shared token
    bucket means they cannot together overrun the rate limit."""
    registry = JobRegistry()
    gate = asyncio.Event()

    async def blocking(job):
        await gate.wait()
        return {}

    refresh_job = await registry.start("refresh", blocking)
    pull_job = await registry.start("asa_pull", blocking)

    assert registry.running("refresh") is refresh_job
    assert registry.running("asa_pull") is pull_job

    gate.set()
    await registry.wait(refresh_job.id)
    await registry.wait(pull_job.id)

    assert registry.get(refresh_job.id).status == "succeeded"
    assert registry.get(pull_job.id).status == "succeeded"


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
    """The bound must evict the oldest finished jobs, not the newest — a
    caller polling GET /jobs for the run it just finished must still find it."""
    registry = JobRegistry(history=3)
    gate = asyncio.Event()

    async def blocking(job):
        await gate.wait()
        return {}

    long_running = await registry.start("refresh", blocking)
    finished_jobs = []
    for _ in range(5):
        finished = await registry.start("rescore", lambda job: _done())
        await registry.wait(finished.id)
        finished_jobs.append(finished)

    assert registry.get(long_running.id) is not None
    assert len(registry.list()) == 4  # 3 finished + the running one
    # The 3 kept must be the most recent, not the first 3 to have run.
    kept_ids = {job.id for job in registry.list()}
    assert kept_ids == {long_running.id} | {job.id for job in finished_jobs[-3:]}
    assert finished_jobs[-1].id in kept_ids

    gate.set()
    await registry.wait(long_running.id)


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
