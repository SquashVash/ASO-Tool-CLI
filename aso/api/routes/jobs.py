"""Long-running work: start it, watch it, stop it.

A full refresh is 2-4 hours at the paced rate, so nothing here can live inside
a request/response cycle. `pipeline.refresh` already fires a callback per
keyword, which is where the progress numbers come from.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ... import pipeline, repository
from ...db import session
from ..jobs import Job, JobConflict
from ..schemas import JobResponse, RefreshRequest

router = APIRouter(tags=["jobs"])


@router.post("/refresh", response_model=JobResponse, status_code=202)
async def start_refresh(body: RefreshRequest, request: Request) -> JobResponse:
    state = request.app.state.aso

    with session() as conn:
        selected = repository.list_keywords(
            conn,
            tag=body.tag,
            country=body.country,
            active_only=not body.include_inactive,
        )
    if body.limit is not None:
        selected = selected[: body.limit]
    if not selected:
        raise HTTPException(
            status_code=422, detail="No keywords match that filter; nothing to refresh"
        )

    async def run(job: Job) -> dict:
        job.total = len(selected)

        def on_progress(outcome: pipeline.KeywordOutcome) -> None:
            job.done += 1
            job.current = outcome.keyword

        # The connection is opened inside the task and lives as long as the
        # run. WAL plus autocommit means readers are never blocked by it.
        with session() as conn:
            report = await pipeline.refresh(
                conn,
                selected,
                force=body.force,
                on_progress=on_progress,
                fetcher=state.fetcher,
            )
        return {
            "succeeded": report.succeeded,
            "failed": report.failed,
            "requests_made": report.requests_made,
            "retries": report.retries,
            "duration_seconds": report.duration_seconds,
        }

    try:
        job = await state.jobs.start("refresh", run, params=body.model_dump())
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobResponse.from_job(job)


@router.get("/jobs", response_model=list[JobResponse])
def list_jobs(request: Request) -> list[JobResponse]:
    return [JobResponse.from_job(job) for job in request.app.state.aso.jobs.list()]


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, request: Request) -> JobResponse:
    job = request.app.state.aso.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    return JobResponse.from_job(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(job_id: str, request: Request) -> JobResponse:
    registry = request.app.state.aso.jobs
    if not await registry.cancel(job_id):
        raise HTTPException(status_code=404, detail=f"No running job {job_id}")
    return JobResponse.from_job(registry.get(job_id))
