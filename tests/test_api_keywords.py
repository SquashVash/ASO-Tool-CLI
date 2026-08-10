"""Read endpoints: stored data only, no network, ever."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from aso import calibration, store as store_module
from aso.api.app import create_app

from .conftest import days_ago


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def seed(keyword="forex", country="us", tags="lcp", opportunity=36.0):
    with store_module.session() as store:
        store.add_keyword(keyword, country, tags)
        row = store.require_keyword(keyword, country)
        store.write_scores(
            row["id"],
            captured_at=days_ago(0),
            search_score=60.0,
            competition_score=40.0,
            opportunity_score=opportunity,
            comp_rating_count=50.0,
            comp_app_power=80.0,
            search_prefix_depth=3,
            search_hint_rank=2,
        )
        return row["id"]


def test_list_returns_latest_scores(client):
    seed()
    body = client.get("/keywords").json()
    assert len(body) == 1
    assert body[0]["keyword"] == "forex"
    assert body[0]["opportunity_score"] == 36.0
    assert body[0]["tags"] == ["lcp"]
    assert body[0]["captured_at"] is not None


def test_list_filters_by_keyword_so_a_caller_can_resolve_an_id(client):
    seed(keyword="forex")
    seed(keyword="candlestick patterns")
    body = client.get("/keywords", params={"keyword": "forex"}).json()
    assert [row["keyword"] for row in body] == ["forex"]


def test_list_keyword_filter_resolves_regardless_of_limit(client):
    """The keyword filter is how a caller holding only the string finds an id.

    That must hold even when `limit` is combined with it: a keyword with a low
    opportunity score should not become unresolvable just because `limit`
    truncated the sorted list before the filter got a chance to look for it.
    """
    seed(keyword="candlestick patterns", opportunity=90.0)
    seed(keyword="forex", opportunity=10.0)
    body = client.get(
        "/keywords", params={"keyword": "forex", "limit": 1}
    ).json()
    assert [row["keyword"] for row in body] == ["forex"]


def test_detail_carries_components_with_their_weights(client):
    keyword_id = seed()
    body = client.get(f"/keywords/{keyword_id}").json()
    assert body["keyword"] == "forex"
    weights = {c["name"]: c["weight"] for c in body["components"]}
    values = {c["name"]: c["value"] for c in body["components"]}
    assert values["comp_app_power"] == 80.0
    assert weights["comp_app_power"] > 0


def test_detail_404s_for_an_unknown_id(client):
    assert client.get("/keywords/9999").status_code == 404


@respx.mock(assert_all_called=False)
def test_read_endpoints_never_touch_the_network(respx_mock, client):
    """The same discipline test_dashboard.py enforces on the dashboard.

    A read path that fetches would fire rate-limited requests on every caller
    poll and reliably earn a 403 mid-refresh.
    """
    blocked = respx_mock.route().mock(
        side_effect=AssertionError("read endpoint made a network request")
    )
    keyword_id = seed()
    for path in (
        "/health",
        "/keywords",
        f"/keywords/{keyword_id}",
        "/tags",
        "/countries",
    ):
        assert client.get(path).status_code == 200
    assert not blocked.called
