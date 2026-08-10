# Keyword API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve `aso` keyword data over HTTP on loopback so other services on the deployment host can read scores, score keywords live, and drive collection runs.

**Architecture:** A FastAPI app in `aso/api/` that is a transport layer over the existing `repository` and `pipeline` functions — the same relationship `cli.py` has to them. The app process owns a single long-lived `aso.http.Fetcher`, which makes its token bucket the one authority on the per-IP rate limit; scheduled collection therefore `curl`s the API rather than invoking the CLI. Long runs become in-memory background jobs with a status endpoint.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, Typer (existing CLI), raw `sqlite3` (existing), pytest + respx (existing).

**Spec:** `docs/superpowers/specs/2026-08-09-keyword-api-design.md`

## Global Constraints

- **Python `>=3.11`**, dependencies managed by `uv`. Run everything as `uv run …`.
- **All SQL stays in `aso/repository.py`.** No `conn.execute` in `aso/api/`. No scoring logic in `aso/api/` either — it calls `pipeline`.
- **One `Fetcher` per process.** Never construct a `Fetcher` inside a request handler. Everything that fetches uses `request.app.state.aso.fetcher`.
- **Single uvicorn worker.** `aso serve` must not expose a `--workers` flag. Two workers means two token buckets (403s from Apple) and two job registries.
- **Bind loopback by default.** `ASO_API_HOST` defaults to `127.0.0.1`, `ASO_API_PORT` to `8081`. There is no authentication, by design.
- **Read handlers are `def`, not `async def`**, so FastAPI runs their blocking SQLite reads in a threadpool. Handlers that fetch or start jobs are `async def`.
- **Every module under `aso/api/` that binds `settings` at import time must be added to `SETTINGS_HOLDERS` in `tests/conftest.py`.** The comment there records that omitting `lookup` made a test pace itself at the real 15 req/min against the real `aso.db`. Task 1 adds a test that enforces this.
- **Timestamps** are ISO-8601 UTC strings via `aso.db.utcnow()`.
- Test command throughout: `uv run pytest`.

---

### Task 1: App skeleton, `/health`, and `aso serve`

**Files:**
- Modify: `pyproject.toml`
- Modify: `aso/config.py` (add `api_host`, `api_port` to `Settings` and `from_env`)
- Create: `aso/api/__init__.py`
- Create: `aso/api/state.py`
- Create: `aso/api/deps.py`
- Create: `aso/api/app.py`
- Create: `aso/api/routes/__init__.py`
- Create: `aso/api/routes/meta.py`
- Modify: `aso/cli.py` (add `serve` command)
- Modify: `tests/conftest.py` (extend `SETTINGS_HOLDERS`)
- Modify: `.env.example`
- Test: `tests/test_api_app.py`

**Interfaces:**
- Consumes: `aso.db.session`, `aso.db.init_db`, `aso.db.applied_versions`, `aso.http.Fetcher`, `aso.config.settings`.
- Produces:
  - `aso.api.state.AppState` — dataclass with `fetcher: Fetcher`, `jobs: JobRegistry | None` (None until Task 7), `chart_indexes: dict[tuple[str, str], ChartIndex]`, `chart_lock: asyncio.Lock`; method `async def chart_index(self, conn, country) -> ChartIndex` (added in Task 5, stubbed absent here).
  - `aso.api.app.create_app() -> FastAPI`
  - `aso.api.deps.get_conn() -> Iterator[sqlite3.Connection]` (FastAPI dependency)
  - `request.app.state.aso` is the `AppState`.

- [ ] **Step 1: Add dependencies**

```bash
uv add fastapi "uvicorn[standard]"
```

Confirm `pyproject.toml`'s `[project].dependencies` now lists `fastapi` and `uvicorn[standard]`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_api_app.py`:

```python
"""The API's skeleton: wiring, settings isolation, and loopback defaults."""

from __future__ import annotations

import importlib
import pkgutil

from fastapi.testclient import TestClient

import aso.api
from aso import config, db
from aso.api.app import create_app

from . import conftest


def test_health_reports_schema_and_counts(tmp_path):
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
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/test_api_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aso.api'`

- [ ] **Step 4: Add the config settings**

In `aso/config.py`, add to the `Settings` dataclass, after `default_country`:

```python
    # The API binds loopback because this tool has no authentication. Other
    # services on the same host can reach it; nothing else can. Reaching it
    # from elsewhere is an SSH tunnel, not a config change.
    api_host: str = "127.0.0.1"
    api_port: int = 8081
```

And in `Settings.from_env`, alongside `default_country`:

```python
            api_host=_env_str("ASO_API_HOST", "127.0.0.1"),
            api_port=_env_int("ASO_API_PORT", 8081),
```

- [ ] **Step 5: Create the app state**

Create `aso/api/__init__.py`:

```python
"""HTTP transport over the same functions the CLI calls.

The API process is the only thing that talks to Apple: it owns a single
long-lived `Fetcher`, so its token bucket is the one authority on the 15
requests/minute the iTunes endpoints allow per IP. A second fetching process
on the same host means a second bucket and a guaranteed 403.
"""
```

Create `aso/api/state.py`:

```python
"""Process-wide state: the shared fetcher, chart indexes, and the job registry.

Held on `app.state.aso` and reachable from any handler via `request.app.state`.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field

from ..clients.charts import ChartIndex
from ..http import Fetcher


@dataclass
class AppState:
    fetcher: Fetcher
    # Keyed by (country, UTC date). The index is a property of the storefront
    # and the day — `charts.CHARTS_TTL_DAYS` expires the SQLite cache daily, so
    # keying on country alone would let a long-running process serve a
    # week-old index from memory and never notice.
    chart_indexes: dict[tuple[str, str], ChartIndex] = field(default_factory=dict)
    chart_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
```

Create `aso/api/deps.py`:

```python
"""FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from ..db import session


def get_conn() -> Iterator[sqlite3.Connection]:
    """One connection per request, closed when it ends.

    `sqlite3` connections are not shareable across threads and read handlers
    run in FastAPI's threadpool, so a process-wide connection would be a bug.
    `db.connect` already sets WAL and a 30s busy timeout, which is what makes a
    read safe alongside a refresh writing snapshots.
    """
    with session() as conn:
        yield conn
```

- [ ] **Step 6: Create the meta route**

Create `aso/api/routes/__init__.py` (empty file) and `aso/api/routes/meta.py`:

```python
"""Liveness and provenance: what database is this, and how current is it."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ... import repository
from ...config import settings
from ...db import applied_versions
from ..deps import get_conn

router = APIRouter(tags=["meta"])


@router.get("/health")
def health(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, object]:
    versions = applied_versions(conn)
    return {
        "status": "ok",
        "db_path": str(settings.db_path),
        "schema_version": max(versions) if versions else 0,
        "keywords": len(repository.list_keywords(conn, active_only=False)),
        "countries": repository.countries(conn),
    }
```

- [ ] **Step 7: Create the app factory**

Create `aso/api/app.py`:

```python
"""App factory and lifespan.

The lifespan owns the one `Fetcher` this process gets. Creating it here rather
than per-request is the whole rate-limit design: see the module docstring in
`aso/api/__init__.py`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..config import settings
from ..db import init_db
from ..http import Fetcher
from .routes import meta
from .state import AppState

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with Fetcher(settings) as fetcher:
        app.state.aso = AppState(fetcher=fetcher)
        logger.info("aso api ready, rate limit %s/min", settings.rate_limit_per_min)
        yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="aso",
        description="Keyword research for the iOS App Store. Loopback only; no auth.",
        lifespan=lifespan,
    )
    app.include_router(meta.router)
    return app
```

- [ ] **Step 8: Extend the test isolation list**

In `tests/conftest.py`, import the API modules and add them to `SETTINGS_HOLDERS`:

```python
from aso.api import app as api_app  # noqa: F401
from aso.api.routes import meta as routes_meta  # noqa: F401
```

```python
SETTINGS_HOLDERS = (
    config, db, cli, http, lookup, pipeline, itunes, hints,
    api_app, routes_meta,
)
```

- [ ] **Step 9: Run the tests**

Run: `uv run pytest tests/test_api_app.py -v`
Expected: PASS (3 tests)

- [ ] **Step 10: Add the `serve` command**

In `aso/cli.py`, add after the `version` command:

```python
@app.command()
def serve(
    host: str = typer.Option(settings.api_host, "--host"),
    port: int = typer.Option(settings.api_port, "--port"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the HTTP API.

    Deliberately no --workers flag. Two workers would mean two token buckets
    against a per-IP rate limit, and two job registries behind one URL.
    """
    import uvicorn

    setup_logging(verbose)
    console.print(f"[green]aso api[/green] http://{host}:{port}")
    uvicorn.run(
        "aso.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        workers=1,
        log_level="info" if verbose else "warning",
    )
```

- [ ] **Step 11: Document the settings**

Append to `.env.example`:

```bash
# --- HTTP API -------------------------------------------------------------
# The API has no authentication, by design. Loopback means only processes on
# this host can reach it. Change this only if you have put authentication in
# front of it.
ASO_API_HOST=127.0.0.1
ASO_API_PORT=8081
```

- [ ] **Step 12: Verify the CLI wiring and full suite**

Run: `uv run aso serve --help`
Expected: shows `--host`, `--port`, `--verbose`, and **no** `--workers`.

Run: `uv run pytest`
Expected: PASS, no regressions.

- [ ] **Step 13: Commit**

```bash
git add pyproject.toml uv.lock aso/config.py aso/api aso/cli.py tests/conftest.py tests/test_api_app.py .env.example
git commit -m "API: app skeleton, health, and aso serve"
```

---

### Task 2: Keyword read endpoints

**Files:**
- Create: `aso/api/schemas.py`
- Create: `aso/api/routes/keywords.py`
- Modify: `aso/api/app.py` (include the router)
- Modify: `tests/conftest.py` (`SETTINGS_HOLDERS` if the new modules bind `settings`)
- Test: `tests/test_api_keywords.py`

**Interfaces:**
- Consumes: `repository.latest_scores`, `repository.list_keywords`, `repository.latest_snapshot`, `repository.snapshot_history`, `repository.latest_serp`, `repository.split_tags`, `config.COMPETITION_WEIGHTS`.
- Produces:
  - `aso.api.schemas.KeywordScore`, `KeywordDetail`, `SnapshotRow`, `SerpRow`, `ComponentWeight`
  - `GET /keywords`, `GET /keywords/{keyword_id}`, `GET /keywords/{keyword_id}/history`, `GET /keywords/{keyword_id}/serp`

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_keywords.py`:

```python
"""Read endpoints: stored data only, no network, ever."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from aso import db, repository as repo
from aso.api.app import create_app

from .test_repository import days_ago


@pytest.fixture
def client():
    db.init_db()
    with TestClient(create_app()) as test_client:
        yield test_client


def seed(keyword="forex", country="us", tags="lcp", opportunity=36.0):
    with db.session() as conn:
        repo.add_keyword(conn, keyword, country, tags)
        row = repo.require_keyword(conn, keyword, country)
        repo.write_snapshot(
            conn,
            repo.SnapshotWrite(
                keyword_id=row["id"],
                captured_at=days_ago(0),
                search_score=60.0,
                competition_score=40.0,
                opportunity_score=opportunity,
                comp_rating_count=50.0,
                comp_app_power=80.0,
                search_prefix_depth=3,
                search_hint_rank=2,
            ),
        )
        repo.write_serp(conn, row["id"], days_ago(0), [111, 222])
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


def test_history_is_oldest_first(client):
    keyword_id = seed()
    with db.session() as conn:
        repo.write_snapshot(
            conn,
            repo.SnapshotWrite(
                keyword_id=keyword_id,
                captured_at=days_ago(7),
                opportunity_score=10.0,
            ),
        )
    body = client.get(f"/keywords/{keyword_id}/history").json()
    assert [row["opportunity_score"] for row in body] == [10.0, 36.0]


def test_serp_returns_the_latest_ranking(client):
    keyword_id = seed()
    body = client.get(f"/keywords/{keyword_id}/serp").json()
    assert [row["rank"] for row in body] == [1, 2]
    assert [row["track_id"] for row in body] == [111, 222]


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
        f"/keywords/{keyword_id}/history",
        f"/keywords/{keyword_id}/serp",
    ):
        assert client.get(path).status_code == 200
    assert not blocked.called
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_api_keywords.py -v`
Expected: FAIL — 404s on every `/keywords` path, since no router is registered.

- [ ] **Step 3: Write the schemas**

Create `aso/api/schemas.py`:

```python
"""Response models.

Every model carrying a score also carries `captured_at`. A caller that cannot
distinguish a fresh score from a three-week-old one will treat stale data as
current, and the whole point of this API is that the caller is a machine.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..repository import split_tags


class ComponentWeight(BaseModel):
    name: str
    value: float | None
    weight: float


class KeywordScore(BaseModel):
    keyword_id: int
    keyword: str
    country: str
    tags: list[str]
    active: bool
    captured_at: str | None
    search_score: float | None
    competition_score: float | None
    opportunity_score: float | None
    fetch_failed: bool
    fetch_error: str | None

    @classmethod
    def from_row(cls, row) -> "KeywordScore":
        return cls(
            keyword_id=row["keyword_id"],
            keyword=row["keyword"],
            country=row["country"],
            tags=split_tags(row["tags"]),
            active=bool(row["active"]),
            captured_at=row["captured_at"],
            search_score=row["search_score"],
            competition_score=row["competition_score"],
            opportunity_score=row["opportunity_score"],
            fetch_failed=bool(row["fetch_failed"]),
            fetch_error=row["fetch_error"],
        )


class SnapshotRow(BaseModel):
    captured_at: str
    search_score: float | None
    competition_score: float | None
    competition_score_raw: float | None
    opportunity_score: float | None
    search_prefix_depth: int | None
    search_hint_rank: int | None
    fetch_failed: bool
    fetch_error: str | None

    @classmethod
    def from_row(cls, row) -> "SnapshotRow":
        return cls(
            captured_at=row["captured_at"],
            search_score=row["search_score"],
            competition_score=row["competition_score"],
            competition_score_raw=row["competition_score_raw"],
            opportunity_score=row["opportunity_score"],
            search_prefix_depth=row["search_prefix_depth"],
            search_hint_rank=row["search_hint_rank"],
            fetch_failed=bool(row["fetch_failed"]),
            fetch_error=row["fetch_error"],
        )


class KeywordDetail(BaseModel):
    keyword_id: int
    keyword: str
    country: str
    tags: list[str]
    active: bool
    latest: SnapshotRow | None
    components: list[ComponentWeight]


class SerpRow(BaseModel):
    rank: int
    track_id: int
    captured_at: str
    track_name: str | None
    seller_name: str | None
    user_rating_count: int | None
    average_user_rating: float | None
    current_version_release_date: str | None

    @classmethod
    def from_row(cls, row) -> "SerpRow":
        return cls(**{key: row[key] for key in cls.model_fields})
```

- [ ] **Step 4: Write the keyword routes**

Create `aso/api/routes/keywords.py`:

```python
"""Reading what is already stored. No network, no writes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from ... import repository
from ...config import COMPETITION_WEIGHTS
from ...repository import split_tags
from ..deps import get_conn
from ..schemas import ComponentWeight, KeywordDetail, KeywordScore, SerpRow, SnapshotRow

router = APIRouter(tags=["keywords"])


def _require_keyword_row(conn: sqlite3.Connection, keyword_id: int) -> sqlite3.Row:
    """Resolve an id to its keyword row, or 404.

    Goes through `repository.list_keywords` rather than running its own SELECT:
    all SQL stays in the repository, without exception.
    """
    for row in repository.list_keywords(conn, active_only=False):
        if row["id"] == keyword_id:
            return row
    raise HTTPException(status_code=404, detail=f"No keyword with id {keyword_id}")


@router.get("/keywords", response_model=list[KeywordScore])
def list_keywords(
    conn: sqlite3.Connection = Depends(get_conn),
    country: str | None = None,
    tag: str | None = None,
    keyword: str | None = Query(
        None,
        description=(
            "Exact match. Keywords are addressed by id in paths because they "
            "contain spaces, unicode, and sometimes '/'; this is how a caller "
            "holding only the string resolves one."
        ),
    ),
    sort: str = "opportunity",
    limit: int | None = None,
    include_inactive: bool = False,
    include_unscored: bool = True,
) -> list[KeywordScore]:
    try:
        rows = repository.latest_scores(
            conn,
            tag=tag,
            country=country,
            sort=sort,
            limit=limit,
            active_only=not include_inactive,
            include_unscored=include_unscored,
        )
    except ValueError as exc:  # unknown sort column
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if keyword is not None:
        wanted = repository.normalize_keyword(keyword)
        rows = [row for row in rows if row["keyword"] == wanted]
    return [KeywordScore.from_row(row) for row in rows]
```

Then the remaining routes, in the same file:

```python
@router.get("/keywords/{keyword_id}", response_model=KeywordDetail)
def keyword_detail(
    keyword_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> KeywordDetail:
    row = _require_keyword_row(conn, keyword_id)
    latest = repository.latest_snapshot(conn, keyword_id)
    components = [
        ComponentWeight(
            name=name,
            value=latest[name] if latest is not None else None,
            weight=weight,
        )
        for name, weight in COMPETITION_WEIGHTS.items()
    ]
    return KeywordDetail(
        keyword_id=row["id"],
        keyword=row["keyword"],
        country=row["country"],
        tags=split_tags(row["tags"]),
        active=bool(row["active"]),
        latest=SnapshotRow.from_row(latest) if latest is not None else None,
        components=components,
    )


@router.get("/keywords/{keyword_id}/history", response_model=list[SnapshotRow])
def keyword_history(
    keyword_id: int,
    limit: int | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[SnapshotRow]:
    _require_keyword_row(conn, keyword_id)
    rows = repository.snapshot_history(conn, keyword_id, limit=limit)
    return [SnapshotRow.from_row(row) for row in rows]


@router.get("/keywords/{keyword_id}/serp", response_model=list[SerpRow])
def keyword_serp(
    keyword_id: int,
    limit: int = 10,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[SerpRow]:
    _require_keyword_row(conn, keyword_id)
    return [SerpRow.from_row(row) for row in repository.latest_serp(conn, keyword_id, limit=limit)]
```

- [ ] **Step 5: Register the router**

In `aso/api/app.py`, import `keywords` alongside `meta` and add:

```python
    app.include_router(keywords.router)
```

- [ ] **Step 6: Update conftest**

Add `aso.api.routes.keywords` and `aso.api.schemas` to the conftest imports and `SETTINGS_HOLDERS` **only if** they bind `settings` at import. As written they do not — the enforcement test from Task 1 tells you the truth. Run it.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_api_keywords.py tests/test_api_app.py -v`
Expected: PASS (all)

- [ ] **Step 8: Commit**

```bash
git add aso/api tests/test_api_keywords.py tests/conftest.py
git commit -m "API: keyword read endpoints"
```

---

### Task 3: Movers, tags, and countries

**Files:**
- Modify: `aso/api/schemas.py` (add `MoverRow`)
- Modify: `aso/api/routes/meta.py`
- Test: `tests/test_api_meta.py`

**Interfaces:**
- Consumes: `repository.score_movers`, `repository.all_tags`, `repository.countries`.
- Produces: `GET /movers`, `GET /tags`, `GET /countries`; `aso.api.schemas.MoverRow`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_meta.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_api_meta.py -v`
Expected: FAIL — 404 on `/movers`, `/tags`, `/countries`.

- [ ] **Step 3: Add the `MoverRow` schema**

Append to `aso/api/schemas.py`:

```python
class MoverRow(BaseModel):
    """A null delta means *not measured then*, never *did not move*."""

    keyword_id: int
    keyword: str
    country: str
    tags: list[str]
    captured_at: str
    baseline_at: str | None
    opportunity_score: float | None
    search_score: float | None
    competition_score: float | None
    opportunity_delta: float | None
    search_delta: float | None
    competition_delta: float | None

    @classmethod
    def from_row(cls, row) -> "MoverRow":
        data = {key: row[key] for key in cls.model_fields if key != "tags"}
        data["tags"] = split_tags(row["tags"])
        return cls(**data)
```

- [ ] **Step 4: Add the routes**

Append to `aso/api/routes/meta.py` (and import `MoverRow` plus `Query`):

```python
@router.get("/movers", response_model=list[MoverRow])
def movers(
    days: int = Query(7, ge=1),
    country: str | None = None,
    tag: str | None = None,
    include_inactive: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[MoverRow]:
    rows = repository.score_movers(
        conn, days=days, country=country, tag=tag, active_only=not include_inactive
    )
    return [MoverRow.from_row(row) for row in rows]


@router.get("/tags", response_model=list[str])
def tags(conn: sqlite3.Connection = Depends(get_conn)) -> list[str]:
    return repository.all_tags(conn)


@router.get("/countries", response_model=list[str])
def countries(conn: sqlite3.Connection = Depends(get_conn)) -> list[str]:
    return repository.countries(conn)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_api_meta.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add aso/api tests/test_api_meta.py
git commit -m "API: movers, tags, countries"
```

---

### Task 4: Keyword write endpoints

**Files:**
- Modify: `aso/api/schemas.py` (add `AddKeywordRequest`, `PatchKeywordRequest`, `AddKeywordResponse`, `DeleteResponse`)
- Modify: `aso/api/routes/keywords.py`
- Test: `tests/test_api_keyword_writes.py`

**Interfaces:**
- Consumes: `repository.add_keyword`, `repository.set_active`, `repository.normalize_tags`, `repository.delete_keyword`, `repository.keyword_footprint`, `db.transaction`.
- Produces: `POST /keywords`, `PATCH /keywords/{keyword_id}`, `DELETE /keywords/{keyword_id}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_keyword_writes.py`:

```python
"""Adding, amending, and deleting tracked keywords."""

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


def test_delete_reports_the_footprint_it_destroyed(client):
    keyword_id = client.post(
        "/keywords", json={"keyword": "forex", "country": "us"}
    ).json()["keyword_id"]
    with db.session() as conn:
        repo.write_snapshot(
            conn,
            repo.SnapshotWrite(
                keyword_id=keyword_id, captured_at=days_ago(0), opportunity_score=1.0
            ),
        )

    response = client.delete(f"/keywords/{keyword_id}")
    assert response.status_code == 200
    assert response.json() == {"keywords": 1, "snapshots": 1, "serps": 0}
    assert client.get(f"/keywords/{keyword_id}").status_code == 404


def test_delete_404s_for_an_unknown_id(client):
    assert client.delete("/keywords/9999").status_code == 404
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_api_keyword_writes.py -v`
Expected: FAIL — 405 Method Not Allowed on POST/PATCH/DELETE.

- [ ] **Step 3: Add the request schemas**

Append to `aso/api/schemas.py`:

```python
class AddKeywordRequest(BaseModel):
    keyword: str
    country: str
    tags: list[str] = []


class AddKeywordResponse(BaseModel):
    keyword_id: int
    created: bool


class PatchKeywordRequest(BaseModel):
    """Both fields optional; omitted means unchanged.

    `tags` REPLACES the tag set, unlike POST /keywords which merges. A caller
    that wants to remove a tag has no other way to say so.
    """

    active: bool | None = None
    tags: list[str] | None = None


class DeleteResponse(BaseModel):
    keywords: int
    snapshots: int
    serps: int
```

- [ ] **Step 4: Add the write routes**

Append to `aso/api/routes/keywords.py` (import `Response`, `transaction`, and the new schemas):

```python
@router.post("/keywords", response_model=AddKeywordResponse)
def add_keyword(
    body: AddKeywordRequest,
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
) -> AddKeywordResponse:
    """Add a tracked keyword, merging tags into an existing one.

    Merging rather than replacing matches `repository.add_keyword`: re-posting
    an overlapping set must be safe to repeat.
    """
    try:
        with transaction(conn):
            keyword_id, created = repository.add_keyword(
                conn, body.keyword, body.country, body.tags
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response.status_code = 201 if created else 200
    return AddKeywordResponse(keyword_id=keyword_id, created=created)


@router.patch("/keywords/{keyword_id}", response_model=KeywordDetail)
def patch_keyword(
    keyword_id: int,
    body: PatchKeywordRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> KeywordDetail:
    _require_keyword_row(conn, keyword_id)
    with transaction(conn):
        if body.active is not None:
            repository.set_active(conn, keyword_id, body.active)
        if body.tags is not None:
            repository.set_tags(conn, keyword_id, body.tags)
    return keyword_detail(keyword_id, conn)


@router.delete("/keywords/{keyword_id}", response_model=DeleteResponse)
def delete_keyword(
    keyword_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> DeleteResponse:
    """Permanent. Prefer PATCH {"active": false}, which is reversible."""
    _require_keyword_row(conn, keyword_id)
    with transaction(conn):
        counts = repository.delete_keyword(conn, keyword_id)
    return DeleteResponse(**counts)
```

- [ ] **Step 5: Add the missing repository function**

`repository.set_tags` does not exist — `add_keyword` merges, and PATCH needs replace. Add it to `aso/repository.py` next to `set_active`:

```python
def set_tags(
    conn: sqlite3.Connection, keyword_id: int, tags: Iterable[str] | str | None
) -> None:
    """Replace a keyword's tag set.

    `add_keyword` merges, because re-importing an overlapping CSV must be safe.
    Replacing is the other half: without it there is no way to remove a tag.
    """
    conn.execute(
        "UPDATE keywords SET tags = ? WHERE id = ?",
        (normalize_tags(tags), keyword_id),
    )
```

- [ ] **Step 6: Test the new repository function**

Append to `tests/test_repository.py`:

```python
def test_set_tags_replaces_rather_than_merging(conn):
    repo.add_keyword(conn, "forex", "us", ["lcp", "swing"])
    row = repo.require_keyword(conn, "forex", "us")
    repo.set_tags(conn, row["id"], ["daily"])
    assert repo.split_tags(repo.require_keyword(conn, "forex", "us")["tags"]) == ["daily"]
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_api_keyword_writes.py tests/test_repository.py -v`
Expected: PASS (all)

- [ ] **Step 8: Commit**

```bash
git add aso/api aso/repository.py tests/test_api_keyword_writes.py tests/test_repository.py
git commit -m "API: keyword write endpoints, plus repository.set_tags"
```

---

### Task 5: Shared fetcher plumbing — `lookup_async`, chart index cache, refresh delta

This task changes existing code so a single long-lived `Fetcher` can be shared. No new endpoints; the next task consumes it.

**Files:**
- Modify: `aso/lookup.py`
- Modify: `aso/pipeline.py:595-660` (`refresh`)
- Modify: `aso/api/state.py`
- Test: `tests/test_lookup.py`, `tests/test_pipeline.py`, `tests/test_api_state.py`

**Interfaces:**
- Produces:
  - `aso.lookup.lookup_async(keyword: str, country: str, *, force: bool = False, fetcher: Fetcher | None = None, charts: ChartIndex | None = None) -> LookupResult`
  - `aso.lookup.lookup(...)` keeps its existing signature and behaviour.
  - `AppState.chart_index(conn: sqlite3.Connection, country: str) -> ChartIndex` (async)
  - `pipeline.refresh` reports `requests_made` / `retries` as a per-run delta when a `fetcher` is passed in.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pipeline.py`:

```python
async def test_refresh_reports_requests_for_this_run_not_the_process(conn, respx_mock):
    """A shared, long-lived fetcher accumulates across runs.

    Reporting its lifetime total would make the second refresh of the day claim
    the first one's traffic.
    """
    from aso.config import settings as live_settings

    repo.add_keyword(conn, "forex", "us")
    rows = repo.list_keywords(conn)

    async with http.Fetcher(live_settings) as fetcher:
        fetcher.requests_made = 500
        fetcher.retries = 7
        report = await pipeline.refresh(conn, rows, fetcher=fetcher)

    assert report.requests_made < 500
    assert report.retries < 7
```

Append to `tests/test_lookup.py`:

```python
async def test_lookup_async_reuses_a_caller_supplied_fetcher(respx_mock):
    """The API owns one Fetcher for the process; lookup must not build its own,
    or its requests would come out of a second token bucket on the same IP."""
    from aso import http, lookup
    from aso.config import settings as live_settings

    async with http.Fetcher(live_settings) as fetcher:
        before = fetcher.requests_made
        await lookup.lookup_async("forex", "us", fetcher=fetcher)
        assert fetcher.requests_made > before


def test_sync_lookup_still_works_for_streamlit():
    """dashboard.py has no event loop to borrow; the wrapper is why."""
    from aso import lookup

    result = lookup.lookup("forex", "us")
    assert result.scored.outcome.keyword == "forex"
```

Note: both lookup tests need the HTTP mocks the existing `tests/test_lookup.py` already sets up. Reuse whatever fixture the surrounding tests in that file use for iTunes and hints responses — read the top of the file first and follow it exactly rather than inventing new mocks.

Create `tests/test_api_state.py`:

```python
"""The process-wide chart index cache."""

from __future__ import annotations

import pytest

from aso import db
from aso.api.state import AppState
from aso.clients.charts import ChartIndex


class CountingCharts:
    def __init__(self) -> None:
        self.calls = 0

    async def index(self, country: str, *, force: bool = False) -> ChartIndex:
        self.calls += 1
        return ChartIndex(country=country, ranks={1: 1})


async def test_chart_index_is_built_once_per_country_per_day(monkeypatch):
    from aso.api import state as state_module

    charts = CountingCharts()
    monkeypatch.setattr(state_module, "ChartsClient", lambda *a, **kw: charts)

    app_state = AppState(fetcher=object())
    db.init_db()
    with db.session() as conn:
        first = await app_state.chart_index(conn, "us")
        second = await app_state.chart_index(conn, "us")

    assert first is second
    assert charts.calls == 1, "48 chart requests must not be repeated per lookup"


async def test_chart_index_is_keyed_by_country(monkeypatch):
    from aso.api import state as state_module

    charts = CountingCharts()
    monkeypatch.setattr(state_module, "ChartsClient", lambda *a, **kw: charts)

    app_state = AppState(fetcher=object())
    db.init_db()
    with db.session() as conn:
        await app_state.chart_index(conn, "us")
        await app_state.chart_index(conn, "gb")

    assert charts.calls == 2
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_api_state.py tests/test_lookup.py -v`
Expected: FAIL — `AttributeError: 'AppState' object has no attribute 'chart_index'` and `module 'aso.lookup' has no attribute 'lookup_async'`.

- [ ] **Step 3: Split `lookup`**

In `aso/lookup.py`, replace the body of `lookup` with an async core plus a wrapper. Keep the existing module docstring; extend it with the second caller.

```python
async def lookup_async(
    keyword: str,
    country: str,
    *,
    force: bool = False,
    fetcher: Fetcher | None = None,
    charts: ChartIndex | None = None,
) -> LookupResult:
    """Score `keyword` live. Writes no keyword, no snapshot, no SERP.

    `fetcher` lets a long-lived caller — the API — supply the one `Fetcher` its
    process owns, so an ad-hoc lookup draws on the same token bucket a refresh
    is using rather than opening a second one against the same IP.

    `charts` is what makes the resulting competition score comparable to a
    stored one. Without it `comp_app_power` is None and `combine()`
    renormalizes over the rest — and that component carries 0.625 of the fitted
    weight, more than every other combined. `aso check` declines to pay 48
    requests for a one-off; a server that holds the index all day should not.
    """
    keyword = keyword.strip()
    country = country.strip().lower()
    if not keyword:
        raise ValueError("keyword cannot be blank")
    if not country:
        raise ValueError("country cannot be blank")

    async def run(active: Fetcher) -> tuple[pipeline.ScoredKeyword, int]:
        before = active.requests_made
        with session() as conn:
            itunes = ITunesClient(active, conn, settings)
            hints = HintsClient(active, conn, settings)
            scored = await pipeline.score_keyword(
                keyword, country, itunes=itunes, hints=hints, force=force, charts=charts
            )
            pipeline.blend_outcome(conn, scored.outcome)
        return scored, active.requests_made - before

    if fetcher is not None:
        scored, requests_made = await run(fetcher)
    else:
        async with Fetcher(settings) as owned:
            scored, requests_made = await run(owned)

    with session() as conn:
        existing = repository.get_keyword(conn, keyword, country)
        percentile, compared = opportunity_percentile(
            conn, scored.outcome.opportunity_score
        )

    return LookupResult(
        scored=scored,
        requests_made=requests_made,
        tracked=existing is not None,
        percentile=percentile,
        compared_against=compared,
    )


def lookup(keyword: str, country: str, *, force: bool = False) -> LookupResult:
    """Synchronous `lookup_async`, for a caller with no event loop to borrow.

    That caller is Streamlit, whose script model reruns the whole module on
    every widget interaction and offers no loop of its own. Runs its own loop
    via `asyncio.run`, so it must not be called from inside one.
    """
    return asyncio.run(lookup_async(keyword, country, force=force))
```

Add the import `from .clients.charts import ChartIndex` at the top of the file.

- [ ] **Step 4: Make `pipeline.refresh` report a delta**

In `aso/pipeline.py`, inside `refresh`, capture the counters before the run and subtract at the end. Replace:

```python
    report.finished_at = utcnow()
    report.requests_made = active_fetcher.requests_made
    report.retries = active_fetcher.retries
```

with:

```python
    report.finished_at = utcnow()
    # A caller-supplied fetcher may be long-lived and shared — the API owns one
    # for the whole process — so its counters accumulate across runs. Report
    # what THIS run cost, not what the process has spent since boot.
    report.requests_made = active_fetcher.requests_made - requests_before
    report.retries = active_fetcher.retries - retries_before
```

and immediately before the `if fetcher is not None:` branch, add:

```python
    requests_before = fetcher.requests_made if fetcher is not None else 0
    retries_before = fetcher.retries if fetcher is not None else 0
```

- [ ] **Step 5: Add the chart index cache to `AppState`**

In `aso/api/state.py`, add the imports and the method:

```python
from datetime import datetime, timezone

from ..clients.charts import ChartIndex, ChartsClient
from ..config import settings
```

```python
    async def chart_index(
        self, conn: sqlite3.Connection, country: str
    ) -> ChartIndex:
        """The storefront's chart index, built at most once per country per day.

        Costs ~48 requests (about 3.5 minutes at the paced rate) the first time
        a storefront is asked for on a given day, and nothing afterwards. The
        lock matters: two lookups arriving together would otherwise each pay
        that price for the same answer.
        """
        key = (country.lower(), datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        async with self.chart_lock:
            cached = self.chart_indexes.get(key)
            if cached is not None:
                return cached
            client = ChartsClient(self.fetcher, conn, settings)
            index = await client.index(country)
            self.chart_indexes[key] = index
            return index
```

Because `state.py` now binds `settings` at import, add `aso.api.state` to the conftest imports and to `SETTINGS_HOLDERS`. The Task 1 enforcement test fails until you do.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_api_state.py tests/test_lookup.py tests/test_pipeline.py tests/test_dashboard.py -v`
Expected: PASS. `test_dashboard.py` must be untouched and still green — the sync `lookup` wrapper is what guarantees that.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest`
Expected: PASS, no regressions.

- [ ] **Step 8: Commit**

```bash
git add aso/lookup.py aso/pipeline.py aso/api/state.py tests/
git commit -m "Share one Fetcher: async lookup core, per-day chart index, per-run request counts"
```

---

### Task 6: `POST /lookup`

**Files:**
- Modify: `aso/api/schemas.py` (add `LookupRequest`, `LookupResponse`)
- Create: `aso/api/routes/lookup.py`
- Modify: `aso/api/app.py`
- Test: `tests/test_api_lookup.py`

**Interfaces:**
- Consumes: `lookup.lookup_async`, `AppState.chart_index`, `AppState.fetcher`.
- Produces: `POST /lookup` → `LookupResponse`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_lookup.py`:

```python
"""Live scoring of an arbitrary keyword."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aso import db
from aso.api.app import create_app
from aso.clients.charts import ChartIndex


@pytest.fixture
def client():
    db.init_db()
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def stub_charts(monkeypatch):
    """No test pays 48 chart requests."""
    from aso.api import state as state_module

    class Stub:
        async def index(self, country, *, force=False):
            return ChartIndex(country=country, ranks={111: 3})

    monkeypatch.setattr(state_module, "ChartsClient", lambda *a, **kw: Stub())


def test_lookup_scores_an_untracked_keyword(client, respx_mock):
    """Set up iTunes and hints mocks exactly as tests/test_lookup.py does."""
    body = client.post("/lookup", json={"keyword": "forex", "country": "us"}).json()
    assert body["keyword"] == "forex"
    assert body["tracked"] is False
    assert body["opportunity_score"] is not None


def test_lookup_includes_comp_app_power(client, respx_mock):
    """Without the chart index this is None and the competition score is not
    comparable to a stored one — comp_app_power carries 0.625 of the weight."""
    body = client.post("/lookup", json={"keyword": "forex", "country": "us"}).json()
    assert body["components"]["comp_app_power"] is not None


def test_a_repeat_lookup_is_free(client, respx_mock):
    """The HTTP response cache is why: SERP and hints hold for 3 days."""
    client.post("/lookup", json={"keyword": "forex", "country": "us"})
    body = client.post("/lookup", json={"keyword": "forex", "country": "us"}).json()
    assert body["requests_made"] == 0


def test_lookup_rejects_a_blank_keyword(client):
    response = client.post("/lookup", json={"keyword": "  ", "country": "us"})
    assert response.status_code == 422
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_api_lookup.py -v`
Expected: FAIL — 404 on `/lookup`.

- [ ] **Step 3: Add the schemas**

Append to `aso/api/schemas.py`:

```python
class LookupRequest(BaseModel):
    keyword: str
    country: str
    force: bool = False


class LookupResponse(BaseModel):
    keyword: str
    country: str
    search_score: float | None
    search_score_proxy: float | None
    search_source: str | None
    competition_score: float | None
    competition_score_raw: float | None
    opportunity_score: float | None
    components: dict[str, float | None]
    tracked: bool
    # Where this score falls among tracked keywords, 0-100. None when nothing
    # is tracked or scoring failed: an unscored keyword has no rank, and 0
    # would read as "the worst one".
    percentile: float | None
    compared_against: int
    # 0 means every response came from the HTTP cache.
    requests_made: int
    failed: bool
    error: str | None

    @classmethod
    def from_result(cls, result) -> "LookupResponse":
        outcome = result.scored.outcome
        return cls(
            keyword=outcome.keyword,
            country=outcome.country,
            search_score=outcome.search_score,
            search_score_proxy=outcome.search_score_proxy,
            search_source=outcome.search_source,
            competition_score=outcome.competition_score,
            competition_score_raw=outcome.competition_score_raw,
            opportunity_score=outcome.opportunity_score,
            components=result.scored.components,
            tracked=result.tracked,
            percentile=result.percentile,
            compared_against=result.compared_against,
            requests_made=result.requests_made,
            failed=outcome.failed,
            error=outcome.error,
        )
```

- [ ] **Step 4: Add the route**

Create `aso/api/routes/lookup.py`:

```python
"""Live scoring of any keyword, tracked or not.

Reads through the same `http_cache` the CLI uses (SERP and autocomplete at a
3-day TTL), so a repeat lookup inside that window costs nothing and returns in
milliseconds. Stored snapshots are never read: the score is always recomputed,
so a weight change in `config.py` shows up on the next call either way.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ... import lookup as lookup_module
from ...db import session
from ..schemas import LookupRequest, LookupResponse

router = APIRouter(tags=["lookup"])


@router.post("/lookup", response_model=LookupResponse)
async def lookup(body: LookupRequest, request: Request) -> LookupResponse:
    state = request.app.state.aso
    try:
        with session() as conn:
            charts = await state.chart_index(conn, body.country)
        result = await lookup_module.lookup_async(
            body.keyword,
            body.country,
            force=body.force,
            fetcher=state.fetcher,
            charts=charts,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LookupResponse.from_result(result)
```

- [ ] **Step 5: Register the router**

In `aso/api/app.py`, import `lookup` from `.routes` and add `app.include_router(lookup.router)`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_api_lookup.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add aso/api tests/test_api_lookup.py
git commit -m "API: POST /lookup with the chart index applied"
```

---

### Task 7: The job registry

No routes yet — this task builds and tests the registry in isolation.

**Files:**
- Create: `aso/api/jobs.py`
- Modify: `aso/api/state.py` (hold a `JobRegistry`)
- Modify: `aso/api/app.py` (construct it, shut it down)
- Test: `tests/test_api_jobs.py`

**Interfaces:**
- Produces:
  - `aso.api.jobs.Job` — dataclass: `id: str`, `kind: str`, `status: str`, `started_at: str`, `finished_at: str | None`, `params: dict`, `done: int`, `total: int | None`, `current: str | None`, `result: dict | None`, `error: str | None`
  - `aso.api.jobs.JobConflict(RuntimeError)`
  - `aso.api.jobs.JobRegistry(history: int = 50)` with `running(kind) -> Job | None`, `async start(kind, run, *, params=None) -> Job`, `get(job_id) -> Job | None`, `list() -> list[Job]`, `async cancel(job_id) -> bool`, `async shutdown() -> None`
  - `run` is `Callable[[Job], Awaitable[dict]]` — it receives its own `Job` so it can update `done`/`total`/`current`, and returns the result dict.
  - `AppState.jobs: JobRegistry`

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_jobs.py`:

```python
"""The in-memory job registry.

Jobs are not persisted. A job IS an asyncio task in this process: if the
process dies the run dies with it, and a stored status='running' row would
outlive the thing it describes and lie to the next caller.
"""

from __future__ import annotations

import asyncio

import pytest

from aso.api.jobs import JobConflict, JobRegistry


async def test_a_job_runs_and_records_its_result():
    registry = JobRegistry()

    async def run(job):
        job.total = 2
        job.done = 2
        return {"succeeded": 2}

    job = await registry.start("refresh", run)
    await registry.wait(job.id)

    assert registry.get(job.id).status == "succeeded"
    assert registry.get(job.id).result == {"succeeded": 2}
    assert registry.get(job.id).finished_at is not None


async def test_a_second_job_of_the_same_kind_is_refused():
    """Two refreshes writing snapshots for an overlapping set is wrong."""
    registry = JobRegistry()
    gate = asyncio.Event()

    async def blocking(job):
        await gate.wait()
        return {}

    await registry.start("refresh", blocking)
    with pytest.raises(JobConflict):
        await registry.start("refresh", blocking)

    gate.set()


async def test_different_kinds_may_run_concurrently():
    """A refresh and an ASA pull write different tables, and the shared token
    bucket means they cannot together overrun the rate limit."""
    registry = JobRegistry()
    gate = asyncio.Event()

    async def blocking(job):
        await gate.wait()
        return {}

    await registry.start("refresh", blocking)
    await registry.start("asa_pull", blocking)
    gate.set()


async def test_a_failing_job_records_the_error_and_does_not_raise():
    registry = JobRegistry()

    async def boom(job):
        raise RuntimeError("apple said no")

    job = await registry.start("refresh", boom)
    await registry.wait(job.id)

    assert registry.get(job.id).status == "failed"
    assert "apple said no" in registry.get(job.id).error


async def test_cancel_marks_the_job_cancelled_and_keeps_partial_progress():
    registry = JobRegistry()
    started = asyncio.Event()

    async def slow(job):
        job.done = 3
        started.set()
        await asyncio.sleep(60)
        return {}

    job = await registry.start("refresh", slow)
    await started.wait()
    assert await registry.cancel(job.id) is True
    await registry.wait(job.id)

    assert registry.get(job.id).status == "cancelled"
    assert registry.get(job.id).done == 3


async def test_cancelling_an_unknown_job_returns_false():
    assert await JobRegistry().cancel("nope") is False


async def test_history_is_bounded_but_never_evicts_a_running_job():
    registry = JobRegistry(history=3)
    gate = asyncio.Event()

    async def blocking(job):
        await gate.wait()
        return {}

    long_running = await registry.start("refresh", blocking)
    for _ in range(5):
        finished = await registry.start("rescore", lambda job: _done())
        await registry.wait(finished.id)

    assert registry.get(long_running.id) is not None
    assert len(registry.list()) <= 4  # 3 finished + the running one
    gate.set()


async def _done():
    return {}


async def test_shutdown_cancels_running_jobs():
    """systemctl restart must not orphan a three-hour refresh."""
    registry = JobRegistry()

    async def slow(job):
        await asyncio.sleep(60)
        return {}

    job = await registry.start("refresh", slow)
    await registry.shutdown()

    assert registry.get(job.id).status == "cancelled"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_api_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aso.api.jobs'`

- [ ] **Step 3: Write the registry**

Create `aso/api/jobs.py`:

```python
"""Background jobs, held in memory.

Deliberately not a table. A job IS an asyncio task in this process — kill the
process and the run dies with it — so a persisted `status='running'` row would
outlive the thing it describes and lie to whoever read it next. A restart
loses the history, and the mitigation is that the history was never the record:
the snapshots in `aso.db` are, plus journald.

One slot per kind. Two refreshes writing snapshots for an overlapping keyword
set is wrong; a refresh alongside an ASA pull is not, because they write
different tables and share one token bucket.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from ..db import utcnow

logger = logging.getLogger(__name__)

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    kind: str
    status: str = RUNNING
    started_at: str = ""
    finished_at: str | None = None
    params: dict = field(default_factory=dict)
    done: int = 0
    total: int | None = None
    current: str | None = None
    result: dict | None = None
    error: str | None = None


class JobConflict(RuntimeError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"A {kind} job is already running")


JobBody = Callable[[Job], Awaitable[dict]]


class JobRegistry:
    def __init__(self, history: int = 50) -> None:
        self._history = history
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def running(self, kind: str) -> Job | None:
        for job in self._jobs.values():
            if job.kind == kind and job.status == RUNNING:
                return job
        return None

    async def start(self, kind: str, run: JobBody, *, params: dict | None = None) -> Job:
        if self.running(kind) is not None:
            raise JobConflict(kind)
        job = Job(id=uuid4().hex, kind=kind, started_at=utcnow(), params=params or {})
        self._jobs[job.id] = job
        self._tasks[job.id] = asyncio.create_task(self._run(job, run))
        self._trim()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda job: job.started_at, reverse=True)

    async def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None:
            return False
        task.cancel()
        return True

    async def wait(self, job_id: str) -> None:
        """Block until a job settles. For tests and for shutdown."""
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _run(self, job: Job, run: JobBody) -> None:
        try:
            job.result = await run(job)
            job.status = SUCCEEDED
        except asyncio.CancelledError:
            job.status = CANCELLED
            logger.info("job %s (%s) cancelled after %s items", job.id, job.kind, job.done)
            raise
        except Exception as exc:  # a failed run must not take the server down
            job.status = FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            logger.exception("job %s (%s) failed", job.id, job.kind)
        finally:
            job.finished_at = utcnow()
            self._tasks.pop(job.id, None)

    def _trim(self) -> None:
        finished = [job for job in self.list() if job.status != RUNNING]
        for job in finished[self._history :]:
            self._jobs.pop(job.id, None)
```

- [ ] **Step 4: Wire it into the app**

In `aso/api/state.py`, add to `AppState`:

```python
    jobs: JobRegistry = field(default_factory=JobRegistry)
```

with `from .jobs import JobRegistry` at the top.

In `aso/api/app.py`, change the lifespan so shutdown cancels running jobs:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with Fetcher(settings) as fetcher:
        state = AppState(fetcher=fetcher)
        app.state.aso = state
        logger.info("aso api ready, rate limit %s/min", settings.rate_limit_per_min)
        try:
            yield
        finally:
            # SIGTERM during a three-hour refresh must not orphan the task.
            await state.jobs.shutdown()
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_api_jobs.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add aso/api tests/test_api_jobs.py
git commit -m "API: in-memory job registry with one slot per kind"
```

---

### Task 8: `POST /refresh` and the job endpoints

**Files:**
- Modify: `aso/api/schemas.py` (add `RefreshRequest`, `JobResponse`)
- Create: `aso/api/routes/jobs.py`
- Modify: `aso/api/app.py`
- Test: `tests/test_api_refresh.py`

**Interfaces:**
- Consumes: `JobRegistry`, `repository.list_keywords`, `pipeline.refresh`, `AppState.fetcher`.
- Produces: `POST /refresh`, `GET /jobs`, `GET /jobs/{job_id}`, `POST /jobs/{job_id}/cancel`; `aso.api.schemas.JobResponse.from_job`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_refresh.py`:

```python
"""Refresh as a background job."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from aso import db, repository as repo
from aso.api.app import create_app


@pytest.fixture
def client():
    db.init_db()
    with db.session() as conn:
        repo.add_keyword(conn, "forex", "us", "lcp")
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def stub_refresh(monkeypatch):
    """Refresh itself is tested in test_pipeline; here only the job wrapper is."""
    from aso import pipeline
    from aso.api.routes import jobs as jobs_routes

    async def fake_refresh(conn, keywords, *, on_progress=None, **kwargs):
        report = pipeline.RefreshReport(started_at="2026-08-09T00:00:00Z")
        for row in keywords:
            outcome = pipeline.KeywordOutcome(
                keyword_id=row["id"], keyword=row["keyword"], country=row["country"]
            )
            report.outcomes.append(outcome)
            if on_progress is not None:
                on_progress(outcome)
        report.finished_at = "2026-08-09T00:01:00Z"
        return report

    monkeypatch.setattr(jobs_routes.pipeline, "refresh", fake_refresh)


def test_refresh_returns_202_with_a_job_id(client, stub_refresh):
    response = client.post("/refresh", json={})
    assert response.status_code == 202
    assert response.json()["kind"] == "refresh"
    assert response.json()["id"]


def test_the_job_reports_progress_and_completion(client, stub_refresh):
    job_id = client.post("/refresh", json={}).json()["id"]

    for _ in range(50):
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] != "running":
            break

    assert body["status"] == "succeeded"
    assert body["done"] == 1
    assert body["total"] == 1
    assert body["result"]["succeeded"] == 1


def test_a_concurrent_refresh_is_refused_with_409(client, monkeypatch):
    from aso.api.routes import jobs as jobs_routes

    async def never_finishes(conn, keywords, *, on_progress=None, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(jobs_routes.pipeline, "refresh", never_finishes)

    assert client.post("/refresh", json={}).status_code == 202
    assert client.post("/refresh", json={}).status_code == 409


def test_a_running_job_can_be_cancelled(client, monkeypatch):
    from aso.api.routes import jobs as jobs_routes

    async def never_finishes(conn, keywords, *, on_progress=None, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(jobs_routes.pipeline, "refresh", never_finishes)

    job_id = client.post("/refresh", json={}).json()["id"]
    assert client.post(f"/jobs/{job_id}/cancel").status_code == 200

    for _ in range(50):
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] != "running":
            break
    assert body["status"] == "cancelled"


def test_refresh_with_no_matching_keywords_is_422(client, stub_refresh):
    """Starting a job that has nothing to do just makes a caller poll for
    nothing."""
    response = client.post("/refresh", json={"tag": "nonexistent"})
    assert response.status_code == 422


def test_jobs_list_and_unknown_job_404(client, stub_refresh):
    client.post("/refresh", json={})
    assert len(client.get("/jobs").json()) == 1
    assert client.get("/jobs/nope").status_code == 404
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_api_refresh.py -v`
Expected: FAIL — 404 on `/refresh`.

- [ ] **Step 3: Add the schemas**

Append to `aso/api/schemas.py`:

```python
class RefreshRequest(BaseModel):
    tag: str | None = None
    country: str | None = None
    limit: int | None = None
    force: bool = False
    include_inactive: bool = False


class JobResponse(BaseModel):
    id: str
    kind: str
    status: str
    started_at: str
    finished_at: str | None
    params: dict
    done: int
    total: int | None
    current: str | None
    result: dict | None
    error: str | None

    @classmethod
    def from_job(cls, job) -> "JobResponse":
        return cls(**{key: getattr(job, key) for key in cls.model_fields})
```

- [ ] **Step 4: Write the job routes**

Create `aso/api/routes/jobs.py`:

```python
"""Long-running work: start it, watch it, stop it.

A full refresh is 2-4 hours at the paced rate, so nothing here can live inside
a request/response cycle. `pipeline.refresh` already fires a callback per
keyword, which is where the progress numbers come from.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ... import pipeline, repository
from ...db import session
from ..jobs import Job, JobConflict
from ..schemas import JobResponse, RefreshRequest

router = APIRouter(tags=["jobs"])


@router.post("/refresh", response_model=JobResponse, status_code=202)
async def start_refresh(body: RefreshRequest, request: Request) -> JobResponse:
    state = request.app.state.aso

    with session() as conn:
        selected = repository.list_keywords(
            conn,
            tag=body.tag,
            country=body.country,
            active_only=not body.include_inactive,
        )
    if body.limit is not None:
        selected = selected[: body.limit]
    if not selected:
        raise HTTPException(
            status_code=422, detail="No keywords match that filter; nothing to refresh"
        )

    async def run(job: Job) -> dict:
        job.total = len(selected)

        def on_progress(outcome: pipeline.KeywordOutcome) -> None:
            job.done += 1
            job.current = outcome.keyword

        # The connection is opened inside the task and lives as long as the
        # run. WAL plus autocommit means readers are never blocked by it.
        with session() as conn:
            report = await pipeline.refresh(
                conn,
                selected,
                force=body.force,
                on_progress=on_progress,
                fetcher=state.fetcher,
            )
        return {
            "succeeded": report.succeeded,
            "failed": report.failed,
            "requests_made": report.requests_made,
            "retries": report.retries,
            "duration_seconds": report.duration_seconds,
        }

    try:
        job = await state.jobs.start("refresh", run, params=body.model_dump())
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobResponse.from_job(job)


@router.get("/jobs", response_model=list[JobResponse])
def list_jobs(request: Request) -> list[JobResponse]:
    return [JobResponse.from_job(job) for job in request.app.state.aso.jobs.list()]


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, request: Request) -> JobResponse:
    job = request.app.state.aso.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    return JobResponse.from_job(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(job_id: str, request: Request) -> JobResponse:
    registry = request.app.state.aso.jobs
    if not await registry.cancel(job_id):
        raise HTTPException(status_code=404, detail=f"No running job {job_id}")
    return JobResponse.from_job(registry.get(job_id))
```

- [ ] **Step 5: Register the router**

In `aso/api/app.py`, import `jobs` from `.routes` and add `app.include_router(jobs.router)`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_api_refresh.py -v`
Expected: PASS (6 tests)

If the polling loops flake, the cause is that `TestClient` runs the app's loop only during a request — the background task advances between calls, which is why the tests poll rather than sleep. Do not add `time.sleep`; increase the loop count.

- [ ] **Step 7: Commit**

```bash
git add aso/api tests/test_api_refresh.py
git commit -m "API: POST /refresh as a job, plus job status and cancel"
```

---

### Task 9: ASA pull, popularity pull, and rescore

**Files:**
- Modify: `aso/api/schemas.py` (add `ASAPullRequest`, `PopularityPullRequest`, `RescoreResponse`)
- Modify: `aso/api/routes/jobs.py`
- Test: `tests/test_api_collection.py`

**Interfaces:**
- Consumes: `pipeline.pull_asa`, `pipeline.pull_apple_popularity`, `pipeline.rescore`, `config.ASA_DEFAULT_LOOKBACK_DAYS`, `settings.apple_popularity_enabled`.
- Produces: `POST /asa/pull`, `POST /popularity/pull`, `POST /rescore`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_collection.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_api_collection.py -v`
Expected: FAIL — 404 on all three paths.

- [ ] **Step 3: Add the schemas**

Append to `aso/api/schemas.py`:

```python
class ASAPullRequest(BaseModel):
    days: int | None = None  # defaults to ASA_DEFAULT_LOOKBACK_DAYS


class PopularityPullRequest(BaseModel):
    country: str
    tag: str | None = None
    limit: int | None = None


class RescoreResponse(BaseModel):
    total: int
    changed: int
    largest_move: float
    largest_move_keyword: str | None
    backfilled_extensions: int
```

- [ ] **Step 4: Add the routes**

Append to `aso/api/routes/jobs.py` (importing `date`, `timedelta`, `Depends`, `sqlite3`, `get_conn`, `settings`, `ASA_DEFAULT_LOOKBACK_DAYS`, and the new schemas):

```python
@router.post("/asa/pull", response_model=JobResponse, status_code=202)
async def start_asa_pull(body: ASAPullRequest, request: Request) -> JobResponse:
    state = request.app.state.aso
    days = body.days if body.days is not None else ASA_DEFAULT_LOOKBACK_DAYS
    end = date.today()
    start = end - timedelta(days=days)

    async def run(job: Job) -> dict:
        with session() as conn:
            report = await pipeline.pull_asa(
                conn, start=start, end=end, fetcher=state.fetcher
            )
        return {
            "campaigns_seen": report.campaigns_seen,
            "campaigns_skipped": report.campaigns_skipped,
            "terms_written": report.terms_written,
            "start": report.start,
            "end": report.end,
            "requests_made": report.requests_made,
        }

    try:
        job = await state.jobs.start("asa_pull", run, params=body.model_dump())
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobResponse.from_job(job)


@router.post("/popularity/pull", response_model=JobResponse, status_code=202)
async def start_popularity_pull(
    body: PopularityPullRequest, request: Request
) -> JobResponse:
    """Apple's keyword popularity index.

    503 rather than a stack trace when the endpoint is not enabled or the
    optional browser extra is missing: both are deployment states, not bugs,
    and the caller can do nothing about either from here.
    """
    if not settings.apple_popularity_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "Apple popularity is off. Set apple_popularity_enabled "
                "(ASO_APPLE_POPULARITY_ENABLED=true) to opt in."
            ),
        )

    state = request.app.state.aso
    with session() as conn:
        rows = repository.list_keywords(conn, tag=body.tag, country=body.country)
    if body.limit is not None:
        rows = rows[: body.limit]
    if not rows:
        raise HTTPException(status_code=422, detail="No keywords match that filter")
    terms = [row["keyword"] for row in rows]

    async def run(job: Job) -> dict:
        job.total = len(terms)
        try:
            with session() as conn:
                report = await pipeline.pull_apple_popularity(
                    conn, terms, country=body.country, fetcher=state.fetcher
                )
        except ImportError as exc:
            raise RuntimeError(
                "The optional browser extra is not installed: "
                "uv sync --extra browser && uv run playwright install chromium"
            ) from exc
        job.done = job.total
        return {
            "requested": report.requested,
            "scored": report.scored,
            "censored": report.censored,
            "related": report.related,
            "from_cache": report.from_cache,
        }

    try:
        job = await state.jobs.start("popularity_pull", run, params=body.model_dump())
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobResponse.from_job(job)


@router.post("/rescore", response_model=RescoreResponse)
def rescore(conn: sqlite3.Connection = Depends(get_conn)) -> RescoreResponse:
    """Recompute every stored score from its saved components.

    Synchronous, and a `def` so it runs in the threadpool: it touches no
    network, so there is nothing to pace and nothing to watch.
    """
    report = pipeline.rescore(conn)
    return RescoreResponse(
        total=report.total,
        changed=report.changed,
        largest_move=report.largest_move,
        largest_move_keyword=report.largest_move_keyword,
        backfilled_extensions=report.backfilled_extensions,
    )
```

Because `jobs.py` now binds `settings`, add `aso.api.routes.jobs` to the conftest imports and `SETTINGS_HOLDERS`. The Task 1 enforcement test will tell you.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_api_collection.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest`
Expected: PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add aso/api tests/test_api_collection.py tests/conftest.py
git commit -m "API: ASA pull, popularity pull, and rescore"
```

---

### Task 10: Deployment units and documentation

**Files:**
- Create: `deploy/aso-api.service`
- Create: `deploy/aso-refresh.service`
- Create: `deploy/aso-refresh.timer`
- Create: `deploy/README.md`
- Modify: `README.md` (add an `## API` section before `## Data model`)

**Interfaces:**
- Consumes: `aso serve`, `POST /refresh`.
- Produces: installable systemd units; user-facing documentation.

- [ ] **Step 1: Write the API service unit**

Create `deploy/aso-api.service`:

```ini
[Unit]
Description=aso keyword API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=aso
Group=aso
WorkingDirectory=/opt/aso
EnvironmentFile=/opt/aso/.env
ExecStart=/usr/local/bin/uv run aso serve
Restart=always
RestartSec=5

# The API binds loopback and there is no authentication, so the process should
# be able to reach as little of the host as possible.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/opt/aso

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Write the refresh timer**

Create `deploy/aso-refresh.service`:

```ini
[Unit]
Description=Nightly aso refresh, via the API
Requires=aso-api.service
After=aso-api.service

[Service]
Type=oneshot
User=aso
# Deliberately NOT `aso refresh`. The CLI would open a second token bucket
# against the same per-IP rate limit as the running API and earn a 403. The
# API process is the only thing that talks to Apple.
ExecStart=/usr/bin/curl -fsS --max-time 30 -X POST \
    -H 'Content-Type: application/json' -d '{}' \
    http://127.0.0.1:8081/refresh
```

Create `deploy/aso-refresh.timer`:

```ini
[Unit]
Description=Run the aso refresh nightly

[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Write the deployment guide**

Create `deploy/README.md`:

````markdown
# Deploying on Ubuntu

Target: a 4GB / 2 vCPU box (`ubuntu-4gb-nbg1-2`). One uvicorn worker is ~100MB
and SQLite's page cache is modest, so memory is not the constraint. **Disk is**
— `aso.db` is around 500MB and grows with every refresh, plus its WAL.

## Install

```bash
sudo useradd --system --home /opt/aso --shell /usr/sbin/nologin aso
sudo mkdir -p /opt/aso && sudo chown aso:aso /opt/aso
sudo -u aso git clone <repo> /opt/aso
cd /opt/aso && sudo -u aso uv sync
```

## Credentials and data

`.env` and the ASA key pair are not in git and must be copied up separately.
Both are secrets:

```bash
scp .env asa-private-key.pem root@<host>:/tmp/
sudo install -o aso -g aso -m 600 /tmp/.env /opt/aso/.env
sudo install -o aso -g aso -m 600 /tmp/asa-private-key.pem /opt/aso/
sudo rm /tmp/.env /tmp/asa-private-key.pem
```

Copy the database up rather than starting cold — it holds the fitted
calibration and all your history:

```bash
scp aso.db root@<host>:/tmp/aso.db
sudo install -o aso -g aso -m 644 /tmp/aso.db /opt/aso/aso.db && sudo rm /tmp/aso.db
```

Stop the API before replacing `aso.db`, and copy the `-wal` and `-shm` files
alongside it if they exist — or checkpoint first with
`sqlite3 aso.db 'PRAGMA wal_checkpoint(TRUNCATE);'`.

## Units

```bash
sudo cp deploy/aso-api.service deploy/aso-refresh.service deploy/aso-refresh.timer \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aso-api.service aso-refresh.timer
curl -s localhost:8081/health | jq
journalctl -u aso-api -f
```

## The optional browser extra

Only `POST /popularity/pull` needs it, and it adds ~400MB plus apt
dependencies. `/popularity/pull` returns 503 with an explanation until then, so
this blocks nothing:

```bash
cd /opt/aso && sudo -u aso uv sync --extra browser
sudo -u aso uv run playwright install --with-deps chromium
```

## Do not run the CLI against Apple while the API is up

`aso list`, `aso show`, and `aso rescore` are safe — they touch no network.
`aso refresh`, `aso check`, `aso asa pull` and `aso apple pull` are not: each
opens its own token bucket, and two buckets on one IP is roughly 30 req/min
against a limit that starts refusing around 20. Use the API for those.
````

- [ ] **Step 4: Document the API in the main README**

Add an `## API` section to `README.md`, immediately before `## Data model`:

````markdown
## API

```bash
uv run aso serve            # http://127.0.0.1:8081
```

Interactive docs at `/docs`. See `deploy/` for running it under systemd.

### It binds loopback, and it is the only thing that fetches

Two properties, and the second is the one that is easy to get wrong.

**Loopback**, because this tool has no authentication by design — the same
reason `.streamlit/config.toml` pins the dashboard to localhost. Other services
on the host can reach it; nothing else can. From a laptop, use an SSH tunnel.

**The only fetcher**, because `aso.http.Fetcher`'s token bucket lives in one
process. Two processes on one IP means two buckets, roughly 30 req/min against
a limit that starts returning 403 around 20. So the API owns a single `Fetcher`
for its lifetime, and the nightly timer POSTs to `/refresh` rather than running
`aso refresh`. The CLI still works — just don't point its fetching commands at
Apple while the API is up.

That single shared bucket is also why a live `/lookup` arriving during a
three-hour refresh returns in seconds rather than waiting for it: the two
interleave request-by-request instead of queueing behind a lock.

### Endpoints

| | |
|---|---|
| `GET /health` | schema version, db path, counts |
| `GET /keywords` | `country`, `tag`, `keyword`, `sort`, `limit`, `include_inactive`, `include_unscored` |
| `GET /keywords/{id}` | latest snapshot, components with weights |
| `GET /keywords/{id}/history` | every snapshot, oldest first |
| `GET /keywords/{id}/serp` | the current top 10 |
| `GET /movers` | `days`, `country`, `tag` |
| `GET /tags`, `GET /countries` | |
| `POST /keywords` | add; re-posting merges tags |
| `PATCH /keywords/{id}` | `active`, `tags` (replaces) |
| `DELETE /keywords/{id}` | permanent; returns what it destroyed |
| `POST /lookup` | score any keyword live |
| `POST /refresh` | 202 + job id |
| `POST /asa/pull`, `POST /popularity/pull` | 202 + job id |
| `POST /rescore` | synchronous |
| `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel` | |

Keywords are addressed by id in paths because they contain spaces, unicode, and
sometimes `/`. `GET /keywords?keyword=…&country=…` is how a caller holding only
the string finds the id.

Every response carrying a score also carries `captured_at`. A machine that
cannot tell a fresh score from a three-week-old one will treat stale data as
current.

### `/lookup` is cached, and pays for the chart index

It reads through the same `http_cache` the CLI does — SERP and autocomplete at
a 3-day TTL — so a repeat lookup inside that window makes zero requests and
returns in milliseconds. `requests_made` in the response tells you which
happened; `"force": true` bypasses it. Stored snapshots are never read, so a
change to `COMPETITION_WEIGHTS` shows up on the next call either way.

Unlike `aso check`, it supplies the storefront chart index, so `comp_app_power`
is present and the competition score is comparable to a tracked keyword's.
That costs ~48 requests once per storefront per day — meaning the first lookup
of the day can take about four minutes if the nightly refresh has not already
warmed it. Set your client timeout accordingly.

### Jobs are in memory

A job is an asyncio task in the API process. Restart the service and running
jobs are cancelled and their records lost — the run's real record was always
the snapshots in `aso.db`, plus journald. The registry keeps the last 50.

One job per kind: a second `POST /refresh` while one is running gets 409, since
two runs writing snapshots for an overlapping set is simply wrong. A refresh
and an ASA pull together are fine.

**Run one worker.** `aso serve` has no `--workers` flag on purpose: two workers
would mean two token buckets and two job registries behind one URL.
````

- [ ] **Step 5: Verify the units parse**

Run: `uv run pytest`
Expected: PASS.

If you have systemd available locally, `systemd-analyze verify deploy/aso-api.service` should be clean. On Windows, skip this — the units are validated on the server at `daemon-reload`.

- [ ] **Step 6: Commit**

```bash
git add deploy README.md
git commit -m "Deployment: systemd units and API documentation"
```

---

## Self-Review Notes

Checked against the spec:

- **Every spec section maps to a task.** Architecture → 1, endpoints → 2/3/4/6/8/9, `/lookup` caching and chart index → 5/6, jobs → 7/8, one-worker constraint → 1 (no `--workers`) and 10 (documented), deployment → 10, verification → the test in every task.
- **The two "changes to existing code"** the spec names (`lookup_async` split, `refresh` delta) are both Task 5, with tests that fail first.
- **The spec's 503-when-extra-missing decision** is Task 9, covering both the disabled flag and the missing import.
- **One addition the spec did not anticipate:** `repository.set_tags` (Task 4). `add_keyword` merges tags, so PATCH had no way to remove one. Added to `repository` rather than the route, since all SQL stays there.
- **One refinement:** the spec says the chart index is held "for the process lifetime". Task 5 keys it by `(country, UTC date)` instead, because the underlying SQLite cache expires daily and a server up for a week would otherwise serve a week-old index from memory.
