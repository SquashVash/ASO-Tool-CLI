"""The API's skeleton: wiring, settings isolation, and loopback defaults."""

from __future__ import annotations

import importlib
import pkgutil

from fastapi.testclient import TestClient

import aso.api
from aso import config, db
from aso.api.app import create_app

from . import conftest


def test_health_reports_schema_and_counts():
    db.init_db()
    with db.session() as conn:
        from aso import repository as repo

        repo.add_keyword(conn, "forex", "us")

    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["keywords"] == 1
    assert body["schema_version"] > 0


def test_api_defaults_to_loopback():
    """No auth by design, so binding every interface would publish the data."""
    assert config.settings.api_host == "127.0.0.1"


def test_every_api_module_binding_settings_is_isolated_in_tests():
    """The trap conftest documents: a module that binds `settings` at import
    time is invisible to the isolation fixture unless it is listed, and a test
    that misses it runs against the real aso.db at the real 15 req/min."""
    for info in pkgutil.walk_packages(aso.api.__path__, prefix="aso.api."):
        module = importlib.import_module(info.name)
        if hasattr(module, "settings"):
            assert module in conftest.SETTINGS_HOLDERS, (
                f"{info.name} binds `settings` at import; add it to "
                "SETTINGS_HOLDERS in tests/conftest.py"
            )
