"""Adding, amending, and deleting tracked keywords."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aso import calibration, store as store_module
from aso.api.app import create_app

from .conftest import days_ago


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def test_add_creates_a_keyword(client):
    response = client.post(
        "/keywords", json={"keyword": "forex", "country": "us", "tags": ["lcp"]}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["keyword_id"] > 0


def test_re_adding_merges_tags_rather_than_replacing_them(client):
    client.post("/keywords", json={"keyword": "forex", "country": "us", "tags": ["lcp"]})
    response = client.post(
        "/keywords", json={"keyword": "forex", "country": "us", "tags": ["swing"]}
    )
    assert response.status_code == 200
    assert response.json()["created"] is False

    body = client.get("/keywords", params={"keyword": "forex"}).json()
    assert sorted(body[0]["tags"]) == ["lcp", "swing"]


def test_add_rejects_a_blank_keyword(client):
    response = client.post("/keywords", json={"keyword": "   ", "country": "us"})
    assert response.status_code == 422


def test_patch_deactivates_without_destroying_history(client):
    keyword_id = client.post(
        "/keywords", json={"keyword": "forex", "country": "us"}
    ).json()["keyword_id"]

    response = client.patch(f"/keywords/{keyword_id}", json={"active": False})
    assert response.status_code == 200
    assert response.json()["active"] is False

    assert client.get("/keywords").json() == []
    assert len(client.get("/keywords", params={"include_inactive": True}).json()) == 1


def test_patch_replaces_tags(client):
    keyword_id = client.post(
        "/keywords", json={"keyword": "forex", "country": "us", "tags": ["lcp"]}
    ).json()["keyword_id"]

    response = client.patch(f"/keywords/{keyword_id}", json={"tags": ["swing"]})
    assert response.json()["tags"] == ["swing"]


def test_delete_reports_what_it_removed(client):
    """A delete says how many keywords went, and nothing else did."""
    keyword_id = client.post(
        "/keywords", json={"keyword": "forex", "country": "us"}
    ).json()["keyword_id"]
    body = client.delete(f"/keywords/{keyword_id}").json()
    assert body == {"keywords": 1}
    assert client.get(f"/keywords/{keyword_id}").status_code == 404


def test_delete_404s_for_an_unknown_id(client):
    assert client.delete("/keywords/9999").status_code == 404
