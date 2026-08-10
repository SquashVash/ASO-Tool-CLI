"""Keyword discovery over HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from aso import store as store_module
from aso.api.app import create_app

from .conftest import FIXTURES
from .test_hints import HINTS_URL

HINTS_BODY = (FIXTURES / "hints_candlestick_us.plist").read_text(encoding="utf-8")
EMPTY_HINTS = (FIXTURES / "hints_empty_us.plist").read_text(encoding="utf-8")


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def mock_hints(body: str = HINTS_BODY, status: int = 200) -> None:
    respx.get(HINTS_URL).mock(
        return_value=httpx.Response(status, text=body if status == 200 else "nope")
    )


@respx.mock
def test_suggest_returns_candidates(client):
    mock_hints()
    body = client.post("/suggest", json={"keyword": "candle", "country": "us"}).json()

    assert body["keyword"] == "candle"
    assert body["candidates"], "the fixture offers suggestions"
    assert body["failed"] is False
    first = body["candidates"][0]
    assert set(first) == {"term", "prefix", "rank", "surfaced_by", "tracked"}


@respx.mock
def test_suggest_probes_every_rung(client):
    """Not the scorer's early-stopping walk — see aso/suggest.py."""
    mock_hints()
    body = client.post("/suggest", json={"keyword": "candle", "country": "us"}).json()
    assert body["prefixes_probed"] == ["candle", "candl", "cand", "can", "ca", "c"]
    assert body["requests_made"] == 6


@respx.mock
def test_suggest_needs_no_chart_index(client):
    """Unlike /lookup, discovery must not pay the 48-request chart bill.

    Nothing mocks the charts endpoint here, so a request to it would raise
    respx's "not mocked" error rather than passing quietly.
    """
    mock_hints()
    assert client.post("/suggest", json={"keyword": "candle", "country": "us"}).status_code == 200


@respx.mock
def test_tracked_terms_are_excluded_by_default(client):
    mock_hints()
    seen = client.post("/suggest", json={"keyword": "candle", "country": "us"}).json()
    term = seen["candidates"][0]["term"]

    with store_module.session() as store:
        store.add_keyword(term, "us")

    body = client.post("/suggest", json={"keyword": "candle", "country": "us"}).json()
    assert term not in [c["term"] for c in body["candidates"]]


@respx.mock
def test_include_tracked_returns_them_flagged(client):
    mock_hints()
    seen = client.post("/suggest", json={"keyword": "candle", "country": "us"}).json()
    term = seen["candidates"][0]["term"]

    with store_module.session() as store:
        store.add_keyword(term, "us")

    body = client.post(
        "/suggest",
        json={"keyword": "candle", "country": "us", "include_tracked": True},
    ).json()
    match = next(c for c in body["candidates"] if c["term"] == term)
    assert match["tracked"] is True


@respx.mock
def test_a_prefix_with_no_suggestions_is_not_a_failure(client):
    mock_hints(EMPTY_HINTS)
    body = client.post("/suggest", json={"keyword": "candle", "country": "us"}).json()
    assert body["failed"] is False
    assert body["candidates"] == []


@respx.mock
def test_a_hints_failure_returns_partial_results(client):
    """Partial results stay partial, exactly as the pipeline promises."""
    respx.get(HINTS_URL).mock(
        side_effect=[
            httpx.Response(200, text=HINTS_BODY),
            httpx.Response(403),
            httpx.Response(403),
        ]
    )
    body = client.post("/suggest", json={"keyword": "candle", "country": "us"}).json()
    assert body["failed"] is True
    assert body["error"]
    assert body["candidates"], "the first rung's suggestions survive"


def test_a_blank_keyword_is_422(client):
    assert client.post("/suggest", json={"keyword": "  ", "country": "us"}).status_code == 422


def test_an_unknown_storefront_is_422(client):
    """Raised before any request — a bad country is not a fetch failure."""
    response = client.post("/suggest", json={"keyword": "candle", "country": "zz"})
    assert response.status_code == 422
    assert "zz" in response.json()["detail"]
