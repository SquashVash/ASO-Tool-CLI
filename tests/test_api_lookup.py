"""Live scoring of an arbitrary keyword, end to end over HTTP."""

from __future__ import annotations

import pytest
import respx
from fastapi.testclient import TestClient

from aso import db
from aso.api.app import create_app

from .test_pipeline import mock_both

# The recorded fixtures `mock_both` serves are for this term.
KEYWORD = "candlestick patterns"


@pytest.fixture
def client():
    db.init_db()
    with TestClient(create_app()) as test_client:
        yield test_client


@respx.mock
def test_lookup_scores_an_untracked_keyword(client):
    mock_both()
    body = client.post("/lookup", json={"keyword": KEYWORD, "country": "us"}).json()
    assert body["keyword"] == KEYWORD
    assert body["tracked"] is False
    assert body["opportunity_score"] is not None
    assert body["requests_made"] > 0


@respx.mock
def test_lookup_includes_comp_app_power(client):
    """The chart index is what makes this score comparable to a stored one.
    Without it the component is None and `combine()` renormalizes over the
    rest — and it carries 0.625 of the fitted weight."""
    mock_both()
    body = client.post("/lookup", json={"keyword": KEYWORD, "country": "us"}).json()
    assert body["components"]["comp_app_power"] is not None


@respx.mock
def test_a_repeat_lookup_is_free(client):
    """SERP and autocomplete responses cache for 3 days and the chart index is
    held in process state, so an identical second lookup must cost nothing."""
    mock_both()
    client.post("/lookup", json={"keyword": KEYWORD, "country": "us"})
    body = client.post("/lookup", json={"keyword": KEYWORD, "country": "us"}).json()
    assert body["requests_made"] == 0


def test_lookup_rejects_a_blank_keyword(client):
    response = client.post("/lookup", json={"keyword": "  ", "country": "us"})
    assert response.status_code == 422
