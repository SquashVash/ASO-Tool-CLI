"""Long-running work: start it, watch it, stop it.

A full refresh is 2-4 hours at the paced rate, so nothing here can live inside
a request/response cycle. `pipeline.refresh` already fires a callback per
keyword, which is where the progress numbers come from.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from ... import pipeline, repository
from ...config import ASA_DEFAULT_LOOKBACK_DAYS, settings
from ...db import session
from ..deps import get_conn
from ..jobs import Job, JobConflict
from ..schemas import (
    ASAPullRequest,
    JobResponse,
    PopularityPullRequest,
    RefreshRequest,
    RescoreResponse,
)

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
    # `cancel()` only calls `task.cancel()`; it does not yield, so the task
    # has provably not resumed yet. Without this yield the response reads
    # back `status: "running"` — a state transition it caused but cannot
    # show. One tick is enough: `_run`'s `except asyncio.CancelledError`
    # unwinds synchronously from here.
    await asyncio.sleep(0)
    return JobResponse.from_job(registry.get(job_id))


@router.post("/asa/pull", response_model=JobResponse, status_code=202)
async def start_asa_pull(body: ASAPullRequest, request: Request) -> JobResponse:
    state = request.app.state.aso
    days = body.days if body.days is not None else ASA_DEFAULT_LOOKBACK_DAYS
    end = date.today()
    start = end - timedelta(days=days)

    async def run(job: Job) -> dict:
        with session() as conn:
            report = await pipeline.pull_asa(
                conn, start=start, end=end, fetcher=state.fetcher
            )
        return {
            "campaigns_seen": report.campaigns_seen,
            "campaigns_skipped": report.campaigns_skipped,
            "terms_written": report.terms_written,
            "start": report.start,
            "end": report.end,
            "requests_made": report.requests_made,
        }

    try:
        job = await state.jobs.start("asa_pull", run, params=body.model_dump())
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobResponse.from_job(job)


@router.post("/popularity/pull", response_model=JobResponse, status_code=202)
async def start_popularity_pull(
    body: PopularityPullRequest, request: Request
) -> JobResponse:
    """Apple's keyword popularity index.

    503 rather than a stack trace when the endpoint is not enabled or the
    optional browser extra is missing: both are deployment states, not bugs,
    and the caller can do nothing about either from here.
    """
    if not settings.apple_popularity_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "Apple popularity is off. Set apple_popularity_enabled "
                "(ASO_APPLE_POPULARITY_ENABLED=true) to opt in."
            ),
        )

    state = request.app.state.aso
    with session() as conn:
        rows = repository.list_keywords(conn, tag=body.tag, country=body.country)
    if body.limit is not None:
        rows = rows[: body.limit]
    if not rows:
        raise HTTPException(status_code=422, detail="No keywords match that filter")
    terms = [row["keyword"] for row in rows]

    async def run(job: Job) -> dict:
        job.total = len(terms)
        # No ImportError translation here: a missing `browser` extra never
        # reaches this frame as one. `apple_transport.BrowserTransport`
        # already catches it at the `playwright` import site and re-raises
        # `ApplePopularityError` naming the install command, which propagates
        # to `JobRegistry._run`'s generic handler and lands in `job.error`
        # readable as-is.
        with session() as conn:
            report = await pipeline.pull_apple_popularity(
                conn, terms, country=body.country, fetcher=state.fetcher
            )
        job.done = job.total
        return {
            "requested": report.requested,
            "scored": report.scored,
            "censored": report.censored,
            "related": report.related,
            "from_cache": report.from_cache,
        }

    try:
        job = await state.jobs.start("popularity_pull", run, params=body.model_dump())
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobResponse.from_job(job)


@router.post("/rescore", response_model=RescoreResponse)
def rescore(conn: sqlite3.Connection = Depends(get_conn)) -> RescoreResponse:
    """Recompute every stored score from its saved components.

    Synchronous, and a `def` so it runs in the threadpool: it touches no
    network, so there is nothing to pace and nothing to watch.
    """
    report = pipeline.rescore(conn)
    return RescoreResponse(
        total=report.total,
        changed=report.changed,
        largest_move=report.largest_move,
        largest_move_keyword=report.largest_move_keyword,
        backfilled_extensions=report.backfilled_extensions,
    )
