"""Movers and the small vocabulary endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aso import db, repository as repo
from aso.api.app import create_app

from .test_repository import days_ago


@pytest.fixture
def client():
    db.init_db()
    with TestClient(create_app()) as test_client:
        yield test_client


def test_movers_reports_a_null_delta_when_there_is_no_baseline(client):
    """Not measured then is a different claim from did not move."""
    with db.session() as conn:
        repo.add_keyword(conn, "forex", "us", "lcp")
        row = repo.require_keyword(conn, "forex", "us")
        repo.write_snapshot(
            conn,
            repo.SnapshotWrite(
                keyword_id=row["id"], captured_at=days_ago(0), opportunity_score=40.0
            ),
        )

    body = client.get("/movers", params={"days": 7}).json()
    assert len(body) == 1
    assert body[0]["opportunity_delta"] is None


def test_movers_computes_a_delta_against_an_older_snapshot(client):
    with db.session() as conn:
        repo.add_keyword(conn, "forex", "us")
        row = repo.require_keyword(conn, "forex", "us")
        repo.write_snapshot(
            conn,
            repo.SnapshotWrite(
                keyword_id=row["id"], captured_at=days_ago(30), opportunity_score=10.0
            ),
        )
        repo.write_snapshot(
            conn,
            repo.SnapshotWrite(
                keyword_id=row["id"], captured_at=days_ago(0), opportunity_score=40.0
            ),
        )

    body = client.get("/movers", params={"days": 7}).json()
    assert body[0]["opportunity_delta"] == 30.0


def test_tags_and_countries(client):
    with db.session() as conn:
        repo.add_keyword(conn, "forex", "us", "lcp")
        repo.add_keyword(conn, "trading", "gb", "other")

    assert client.get("/tags").json() == ["lcp", "other"]
    assert client.get("/countries").json() == ["gb", "us"]
