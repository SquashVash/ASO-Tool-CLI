"""Refresh as a background job."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from aso import db, repository as repo
from aso.api.app import create_app


@pytest.fixture
def client():
    db.init_db()
    with db.session() as conn:
        repo.add_keyword(conn, "forex", "us", "lcp")
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def stub_refresh(monkeypatch):
    """Refresh itself is tested in test_pipeline; here only the job wrapper is."""
    from aso import pipeline
    from aso.api.routes import jobs as jobs_routes

    async def fake_refresh(conn, keywords, *, on_progress=None, **kwargs):
        report = pipeline.RefreshReport(started_at="2026-08-09T00:00:00Z")
        for row in keywords:
            outcome = pipeline.KeywordOutcome(
                keyword_id=row["id"], keyword=row["keyword"], country=row["country"]
            )
            report.outcomes.append(outcome)
            if on_progress is not None:
                on_progress(outcome)
        report.finished_at = "2026-08-09T00:01:00Z"
        return report

    monkeypatch.setattr(jobs_routes.pipeline, "refresh", fake_refresh)


def test_refresh_returns_202_with_a_job_id(client, stub_refresh):
    response = client.post("/refresh", json={})
    assert response.status_code == 202
    assert response.json()["kind"] == "refresh"
    assert response.json()["id"]


def test_the_job_reports_progress_and_completion(client, stub_refresh):
    job_id = client.post("/refresh", json={}).json()["id"]

    for _ in range(50):
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] != "running":
            break

    assert body["status"] == "succeeded"
    assert body["done"] == 1
    assert body["total"] == 1
    assert body["result"]["succeeded"] == 1


def test_a_concurrent_refresh_is_refused_with_409(client, monkeypatch):
    from aso.api.routes import jobs as jobs_routes

    async def never_finishes(conn, keywords, *, on_progress=None, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(jobs_routes.pipeline, "refresh", never_finishes)

    assert client.post("/refresh", json={}).status_code == 202
    assert client.post("/refresh", json={}).status_code == 409


def test_a_running_job_can_be_cancelled(client, monkeypatch):
    from aso.api.routes import jobs as jobs_routes

    async def never_finishes(conn, keywords, *, on_progress=None, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(jobs_routes.pipeline, "refresh", never_finishes)

    job_id = client.post("/refresh", json={}).json()["id"]
    assert client.post(f"/jobs/{job_id}/cancel").status_code == 200

    for _ in range(50):
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] != "running":
            break
    assert body["status"] == "cancelled"


def test_refresh_with_no_matching_keywords_is_422(client, stub_refresh):
    """Starting a job that has nothing to do just makes a caller poll for
    nothing."""
    response = client.post("/refresh", json={"tag": "nonexistent"})
    assert response.status_code == 422


def test_jobs_list_and_unknown_job_404(client, stub_refresh):
    client.post("/refresh", json={})
    assert len(client.get("/jobs").json()) == 1
    assert client.get("/jobs/nope").status_code == 404
