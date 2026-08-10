from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from aso import cache, cli, config, db, http, lookup, pipeline, repository  # noqa: F401
from aso.api import app as api_app  # noqa: F401
from aso.api.routes import meta as routes_meta  # noqa: F401
from aso.clients import hints, itunes

FIXTURES = Path(__file__).parent / "fixtures"

# Modules that bound `settings` / `default_settings` at import time. Patching
# `config.settings` alone would miss them, and a test would quietly run against
# the real database at real rate-limit pacing.
#
# `lookup` is on this list because it builds its own Fetcher: omitting it made
# a lookup test pace itself at the production 15 req/min against the real
# aso.db, which is exactly the failure this tuple exists to prevent.
SETTINGS_HOLDERS = (
    config, db, cli, http, lookup, pipeline, itunes, hints,
    api_app, routes_meta,
)


@pytest.fixture(autouse=True)
def isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Keep every test off the real database and off the real clock.

    Two things this guarantees, for the whole suite:

    * `db_path` points at a per-test temp file, so no test can touch the
      developer's `aso.db`.
    * The token bucket and retry backoff are effectively instant. Without this
      the CLI tests would run at the production 15 req/min — a single
      `aso refresh` of one keyword makes about a dozen requests, i.e. most of
      a minute of real sleeping per test.

    Networking is still blocked by respx in the tests that need it; this
    fixture only removes the waiting.
    """
    database = tmp_path / "aso-test.db"
    patched = dataclasses.replace(
        config.settings,
        db_path=database,
        rate_limit_per_min=600_000,
        retry_attempts=2,
    )
    for module in SETTINGS_HOLDERS:
        for name in ("settings", "default_settings"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, patched)

    monkeypatch.setattr(http, "RETRY_INITIAL_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(http, "RETRY_MAX_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(http, "RETRY_JITTER_SECONDS", 0.0)

    yield database


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated, throwaway database."""
    connection = db.connect(tmp_path / "test.db")
    db.migrate(connection)
    try:
        yield connection
    finally:
        connection.close()
