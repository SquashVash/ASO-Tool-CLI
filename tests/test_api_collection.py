"""The other collection jobs, and the synchronous rescore."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from aso import config, db, repository as repo
from aso.api.app import create_app


@pytest.fixture
def client():
    db.init_db()
    with db.session() as conn:
        repo.add_keyword(conn, "forex", "us", "lcp")
    with TestClient(create_app()) as test_client:
        yield test_client


def test_popularity_pull_is_503_when_the_endpoint_is_not_enabled(client):
    """Off by default: an unverified endpoint should be opted into."""
    response = client.post("/popularity/pull", json={"country": "us"})
    assert response.status_code == 503
    assert "apple_popularity_enabled" in response.json()["detail"]


def test_asa_pull_starts_a_job(client, monkeypatch):
    from aso import pipeline
    from aso.api.routes import jobs as jobs_routes

    async def fake_pull(conn, *, start, end, **kwargs):
        return pipeline.ASAPullReport(
            campaigns_seen=1, terms_written=5, start=str(start), end=str(end)
        )

    monkeypatch.setattr(jobs_routes.pipeline, "pull_asa", fake_pull)

    response = client.post("/asa/pull", json={"days": 7})
    assert response.status_code == 202

    job_id = response.json()["id"]
    for _ in range(50):
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] != "running":
            break
    assert body["status"] == "succeeded"
    assert body["result"]["terms_written"] == 5


def test_rescore_is_synchronous_and_needs_no_network(client):
    response = client.post("/rescore")
    assert response.status_code == 200
    assert "total" in response.json()


def test_popularity_pull_job_records_a_readable_error_when_the_browser_extra_is_missing(
    client, monkeypatch
):
    """Covers the path the route used to (wrongly) translate itself.

    `apple_transport.BrowserTransport` catches the raw `ImportError` at the
    `playwright` import site and re-raises `ApplePopularityError` naming the
    install command; that propagates through the job body untouched and lands
    in `job.error` via `JobRegistry._run`'s generic handler.
    """
    from aso.api.routes import jobs as jobs_routes
    from aso.clients.apple_transport import ApplePopularityError

    monkeypatch.setattr(
        jobs_routes,
        "settings",
        dataclasses.replace(config.settings, apple_popularity_enabled=True),
    )

    message = (
        "The browser transport needs Playwright, which is an optional extra:\n"
        "    uv sync --extra browser\n"
        "    uv run playwright install chromium\n"
    )

    async def fake_pull(conn, keywords, *, country, **kwargs):
        raise ApplePopularityError(message)

    monkeypatch.setattr(jobs_routes.pipeline, "pull_apple_popularity", fake_pull)

    response = client.post("/popularity/pull", json={"country": "us"})
    assert response.status_code == 202

    job_id = response.json()["id"]
    for _ in range(50):
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] != "running":
            break
    assert body["status"] == "failed"
    assert "ApplePopularityError" in body["error"]
    assert message in body["error"]
