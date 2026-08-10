# Keyword API: one process owns the rate limit

Date: 2026-08-09
Status: designed, not implemented

## Problem

The tool is about to run on a Hetzner `ubuntu-4gb-nbg1-2` box alongside other
services, and those services need to ask it about keywords over HTTP.

The naive shape — an HTTP wrapper next to a cron'd `aso refresh` — breaks on
the constraint the whole fetch layer is built around. `aso.http.Fetcher` paces
at 15 requests/minute because the iTunes endpoints start returning 403 above
roughly 20/min **per IP**. That budget is enforced by a token bucket held in a
single process. Two processes on one IP means two buckets and ~30/min, which
is not a tuning problem; it is a guaranteed 403 mid-refresh.

Three further constraints shape everything below:

- A full refresh is 2–4 hours at that pacing. Nothing that long can live in a
  request/response cycle.
- `aso.db` is 492MB of SQLite, and the tool has **no authentication by
  design** — the README says so, and `.streamlit/config.toml` pins the
  dashboard to loopback as the consequence.
- `lookup.lookup()` calls `asyncio.run`, so it raises if called from inside a
  running event loop. Every FastAPI handler is inside one.

## Decision

**The API process is the only thing that talks to Apple.** One long-lived
`Fetcher`, created in the app's lifespan and shared by every code path that
fetches, so there is exactly one token bucket and it is the truth. Scheduled
collection runs by `curl`ing the API, not by invoking the CLI.

The CLI keeps working for manual use. What is given up is the freedom to
*schedule* it alongside the API, and to run it by hand during a refresh.

Rejected alternatives:

- **A cross-process lease row in `aso.db`.** Correct even when someone runs the
  CLI by hand mid-refresh, but it is real machinery and introduces a new way to
  stall: a stale lease after a hard kill.
- **A static split of the budget** (API 10/min, CLI 5/min). Trivial, and wastes
  most of the budget whenever only one of them is running — which is almost
  always.

Two decisions follow from "one shared Fetcher" and are worth naming, because
each is a strict improvement over the obvious alternative:

**A shared bucket, not a global mutex.** A mutex around network access would
make a live `/lookup` arriving during a 3-hour refresh wait 3 hours. Sharing
the `Fetcher` instead means the lookup queues for *tokens* behind the refresh's
next request and returns in seconds. `pipeline.refresh` already accepts
`fetcher=`, which is exactly this.

**`/lookup` pays for the chart index.** `lookup()` today passes no `charts`, so
`comp_app_power` is `None` and `combine()` renormalizes over the rest. That
component carries 0.625 of the fitted weight — more than every other component
combined — so an ad-hoc score is built from ~37% of the model, rescaled, and is
not comparable to a tracked keyword's stored score. `aso check` accepts that
rather than pay 48 requests for a one-off. A server should not: the index is
per-storefront-per-day and already SQLite-cached, so it is ~3.5 minutes once a
day per country and free for every lookup after. The mismatch matters more here
because a machine consuming the number cannot eyeball that it looks wrong.

## Scope

Read, live, and write/control. Specifically: serve stored data; score arbitrary
untracked keywords live; add, amend, and delete tracked keywords; and start and
observe collection runs.

## Design

| Piece | Where | Touches network |
|---|---|---|
| 1. App factory + lifespan | `aso/api/app.py` | owns the Fetcher |
| 2. Per-request connection | `aso/api/deps.py` | no |
| 3. Response models | `aso/api/schemas.py` | no |
| 4. Job registry | `aso/api/jobs.py` | no |
| 5. Routes | `aso/api/routes/` | lookup + job starts |
| 6. `aso serve` | `aso/cli.py` | no |

Dependencies added: `fastapi`, `uvicorn[standard]`.

`repository.py`, `pipeline.py`, and `dashboard.py` keep their shape. The API is
a transport layer over functions that already exist, the same way `cli.py` is.
All SQL stays in `aso.repository`; no scoring lives in `aso/api/`.

### Two changes to existing code

**`lookup.lookup()` splits.** An `async def lookup_async(..., fetcher=None,
charts=None)` core, with the existing sync `lookup()` kept as a thin
`asyncio.run` wrapper over it. `dashboard.py` and its tests are untouched. The
docstring's "Streamlit has no loop to borrow" reasoning stays true; it gains a
second caller that does have one.

**`pipeline.refresh` reports a delta.** It currently sets
`report.requests_made = active_fetcher.requests_made`, which on a long-lived
shared fetcher is the process's lifetime total rather than the run's. When a
fetcher is passed in, record before/after and report the difference.

### Threading and SQLite

Read handlers are `def`, not `async def`, so FastAPI runs them in a threadpool
and a blocking SQLite read never stalls the event loop mid-refresh. Each opens
its own connection via `db.session()`: `sqlite3` connections are not
thread-shareable, and `connect()` already sets WAL plus a 30s busy timeout for
precisely the reader-alongside-writer case the dashboard created.

### Endpoints

Keywords are addressed by integer id in paths, never by string — they contain
spaces, unicode, and can contain `/`, which no amount of encoding makes
pleasant in a path segment. A caller holding only the string resolves it with
`GET /keywords?keyword=…&country=…`.

| Endpoint | Notes |
|---|---|
| `GET /health` | schema version, db path, row counts |
| `GET /keywords` | `country`, `tag`, `keyword`, `sort`, `limit`, `include_unscored` → `repository.latest_scores` |
| `GET /keywords/{id}` | latest snapshot, six competition components **with weights**, raw ladder observation, demand source |
| `GET /keywords/{id}/history` | `repository.snapshot_history` |
| `GET /keywords/{id}/serp` | `repository.latest_serp` |
| `GET /movers` | `days`, `country`, `tag` → `repository.score_movers` |
| `GET /tags`, `GET /countries` | |
| `POST /lookup` | live score, tracked or not |
| `POST /keywords` | add tracked keyword |
| `PATCH /keywords/{id}` | `active`, `tags` |
| `DELETE /keywords/{id}` | returns the deleted footprint |
| `POST /refresh` | → job |
| `POST /asa/pull`, `POST /popularity/pull` | → job |
| `POST /rescore` | synchronous: no network, pure SQLite recompute |
| `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel` | |

`POST /popularity/pull` returns 503 with an explicit message when the `browser`
extra is not installed, rather than a stack trace from an import that was never
going to succeed — see Deployment, where that extra is deferred.

Every response carrying a score also carries `captured_at`. A caller that
cannot distinguish a fresh score from a three-week-old one will treat stale
data as current.

### `/lookup` and caching

`score_keyword` reads through the same `http_cache` the CLI uses — SERP and
autocomplete at a 3-day TTL, app metadata at 7 days. A repeat lookup inside
that window makes zero network requests and returns in milliseconds; `force`
bypasses it. The response carries `requests_made` so a caller can tell a cache
hit from a live fetch.

Stored snapshots are never read for this. The score is always recomputed from
the responses, so a weight change in `config.py` is reflected on the next
lookup whether or not the fetch was cached.

The `ChartIndex` is built lazily per storefront and held for the process
lifetime, on top of its existing SQLite cache and daily TTL. Worst case — first
lookup for a storefront on a given day, cold index — is ~4 minutes, so callers
need a generous client timeout. In practice the nightly refresh warms it first.

### Jobs

**In memory, not in a table.** A job *is* an asyncio task in this process; if
the process dies the run dies with it, and a persisted `status='running'` row
would outlive the thing it describes and lie to the next caller. The registry
holds the last 50 records. A restart loses that history, and the mitigation is
that the history was never the record — the snapshots in `aso.db` are, plus
journald.

A record carries `id`, `kind`, `status` (`running` / `succeeded` / `failed` /
`cancelled`), `started_at`, `finished_at`, the params it started with,
`progress` (`done` / `total` / `current`), and on completion the outcome counts,
`requests_made`, and `retries`. Progress needs no new plumbing:
`pipeline.refresh` already takes an `on_progress` callback fired per keyword.

**One slot per kind.** A second `POST /refresh` while one runs returns 409 —
two runs writing snapshots for an overlapping keyword set is wrong. A refresh
and an ASA pull concurrently is allowed: different tables, WAL handles it, and
the shared bucket means they cannot together overrun the rate limit.

**Cancellation.** `refresh` loops keyword-by-keyword with awaits between, so
`task.cancel()` lands in a gap. Snapshots already committed stay committed and
the job reports `cancelled` with partial counts. The lifespan cancels running
jobs on SIGTERM, so `systemctl restart` orphans nothing.

### One worker, as a correctness requirement

Two uvicorn workers means two token buckets (403s) and two job registries (a
`GET /jobs/{id}` that 404s depending on which worker answers). `aso serve` does
not expose a `--workers` flag.

## Deployment

`/opt/aso`, owned by a dedicated `aso` system user, installed with `uv sync`.

- **`aso-api.service`** — `uv run aso serve`, bound `127.0.0.1:8081`,
  `Restart=always`, `EnvironmentFile=/opt/aso/.env`, logging to journald.
  `ASO_API_HOST` / `ASO_API_PORT` in `config.py` default to loopback.
- **`aso-refresh.timer`** — a oneshot that
  `curl -fsS -XPOST localhost:8081/refresh`. Deliberately *not* `aso refresh`;
  routing collection through the API is the point of the shared-bucket
  decision.

**Access is loopback, no auth.** Only processes on the box can connect, which
is the stated requirement. This matches the dashboard's existing posture and
means there is no token to leak or rotate. Reaching it from a laptop is an SSH
tunnel, not a config change.

**Resources.** One uvicorn worker is ~100MB and SQLite's page cache is modest,
so 4GB / 2 vCPU is comfortable. The constraint to watch is disk: `aso.db` is
492MB today and grows with every refresh, plus WAL.

**Data.** `scp` the existing `aso.db` up rather than starting cold — it holds
the fitted calibration and all history. `.env` and `asa-private-key.pem` go up
separately at mode 600 owned by `aso`. `.gitignore` already covers `*.pem` and
`*.db`; re-verify before committing anything.

**The `browser` extra is opt-in and deferred.** Playwright plus Chromium is
~400MB and a pile of apt deps on a headless box, and only
`aso popularity pull` needs it. Decide after the API is up; it blocks nothing.

## Verification

`tests/test_api.py`, on FastAPI's `TestClient`, reusing the autouse
`isolated_environment` fixture.

**The trap conftest already documents:** every `aso.api.*` module that binds
`settings` at import time must be added to `SETTINGS_HOLDERS`. The comment
there records that omitting `lookup` made a test pace itself at the real
15 req/min against the real `aso.db`. The API has the same failure mode across
more modules.

- Read endpoints make zero network calls, asserted with `respx` — the same
  discipline `test_dashboard.py` enforces on the dashboard.
- Job lifecycle: start → `running` → `succeeded`, against a stubbed refresh.
- A duplicate refresh returns 409.
- Cancel yields `cancelled` with partial counts intact.
- `/lookup` on a warm cache reports `requests_made == 0`.
- `/lookup` passes a `ChartIndex`, so `comp_app_power` is present.
- `aso serve` defaults to `127.0.0.1`, asserted as the dashboard's loopback
  binding is.

## Open

- Whether the nightly refresh should be tag-scoped or whole-set. Depends on how
  many keywords are tracked by the time this deploys; a full run at 2–4 hours
  is fine overnight, and a tag-scoped run is a parameter, not a redesign.
- Whether to snapshot `aso.db` before each refresh. One `sqlite3 .backup` line
  against a 492MB file holding fitted calibration is cheap insurance, but it is
  not needed for the API to work.
