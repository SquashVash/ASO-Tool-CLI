from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest

from aso import cache, calibration, cli, config, http, lookup, pipeline  # noqa: F401
from aso import store as store_module  # noqa: F401
from aso.api import app as api_app  # noqa: F401
from aso.api import deps as api_deps  # noqa: F401
from aso.api import state as api_state  # noqa: F401
from aso.api.routes import jobs as routes_jobs  # noqa: F401
from aso.api.routes import keywords as routes_keywords  # noqa: F401
from aso.api.routes import meta as routes_meta  # noqa: F401
from aso.clients import hints, itunes

FIXTURES = Path(__file__).parent / "fixtures"

# Modules that bound `settings` / `default_settings` at import time. Patching
# `config.settings` alone would miss them, and a test would quietly run against
# the real data directory at real rate-limit pacing.
#
# `lookup` is on this list because it builds its own Fetcher: omitting it made
# a lookup test pace itself at the production 15 req/min against the real
# data files, which is exactly the failure this tuple exists to prevent.
SETTINGS_HOLDERS = (
    config, cli, http, lookup, pipeline, store_module, calibration, itunes, hints,
    api_app, api_deps, api_state, routes_meta, routes_jobs, routes_keywords,
)


@pytest.fixture(autouse=True)
def isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Keep every test off the real data files and off the real clock.

    Three things this guarantees, for the whole suite:

    * `data_dir` points at a per-test temp directory, so no test can read or
      overwrite the developer's keyword list, observations or fitted bridges.
    * The response cache starts empty. It is process-wide now rather than a
      table in a per-test database, so without this a cached SERP would leak
      from one test into the next and a "did it refetch?" assertion would pass
      or fail depending on test order.
    * The token bucket and retry backoff are effectively instant. Without this
      the CLI tests would run at the production 15 req/min — a single
      `aso refresh` of one keyword makes about a dozen requests, i.e. most of
      a minute of real sleeping per test.

    Networking is still blocked by respx in the tests that need it; this
    fixture only removes the waiting.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    patched = dataclasses.replace(
        config.settings,
        data_dir=data_dir,
        rate_limit_per_min=600_000,
        retry_attempts=2,
    )
    for module in SETTINGS_HOLDERS:
        for name in ("settings", "default_settings"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, patched)

    cache.default_cache.clear()

    monkeypatch.setattr(http, "RETRY_INITIAL_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(http, "RETRY_MAX_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(http, "RETRY_JITTER_SECONDS", 0.0)

    yield data_dir


@pytest.fixture
def data_dir(isolated_environment: Path) -> Path:
    """The per-test data directory, named for tests that write files into it."""
    return isolated_environment


@pytest.fixture
def store(isolated_environment: Path) -> store_module.Store:
    """An empty, throwaway keyword store pointed at the test data directory.

    Not saved automatically. A test that wants the file on disk calls `.save()`
    itself, which keeps "did this write?" assertions honest.
    """
    return store_module.Store.load()


def days_ago(n: int) -> str:
    """An ISO-8601 UTC timestamp `n` days in the past.

    Lived in test_repository.py, which went with the snapshot history. Several
    suites still need a stamp that is definitely older than a TTL.
    """
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) - timedelta(days=n)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
