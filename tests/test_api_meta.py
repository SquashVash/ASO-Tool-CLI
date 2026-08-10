"""Health and the small vocabulary endpoints."""

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




def test_tags_and_countries(client):
    with store_module.session() as store:
        store.add_keyword("forex", "us", "lcp")
        store.add_keyword("trading", "gb", "other")

    assert client.get("/tags").json() == ["lcp", "other"]
    assert client.get("/countries").json() == ["gb", "us"]
