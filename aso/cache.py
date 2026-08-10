"""On-disk response cache, backed by the `http_cache` table.

In SQLite rather than in memory so runs are resumable. TTLs come from config:
SERPs 3 days, autocomplete 3 days, app metadata 7 days (the last of those lives
on `apps.fetched_at`, not here — see `clients/itunes.py`).

Bodies are stored exactly as received. Reparsing is cheap; refetching costs
four seconds of rate-limit budget.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import parse_ts, utcnow


@dataclass(frozen=True)
class CachedBody:
    body: str
    fetched_at: str


def serp_key(term: str, country: str, limit: int) -> str:
    return f"serp:{country.lower()}:{limit}:{term.strip().casefold()}"


def popularity_key(keyword: str, country: str) -> str:
    """Cache key for one keyword's Apple popularity reading.

    Keyed per keyword rather than per batch on purpose: batches are assembled
    from whatever is uncached at the time, so a batch-shaped key would miss on
    every run whose keyword set differed by one term.
    """
    return f"popularity:{country.lower()}:{keyword.strip().casefold()}"


def hints_key(prefix: str, country: str) -> str:
    return f"hints:{country.lower()}:{prefix.strip().casefold()}"


def charts_key(country: str, feed: str, genre: int) -> str:
    """Cache key for one top-chart feed.

    Keyed per (feed, genre) rather than per assembled index so that one failing
    genre costs one request on the next run instead of invalidating all 48.
    """
    return f"charts:{country.lower()}:{feed}:{genre}"


def get(conn: sqlite3.Connection, key: str, ttl_days: int) -> CachedBody | None:
    """Return the cached body if present and younger than `ttl_days`.

    A stale row is left in place rather than deleted: if the refetch fails, the
    stale copy is still there for a later run, and `purge_expired()` cleans up
    on demand.
    """
    row = conn.execute(
        "SELECT body, fetched_at FROM http_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    if is_expired(row["fetched_at"], ttl_days):
        return None
    return CachedBody(body=row["body"], fetched_at=row["fetched_at"])


def put(conn: sqlite3.Connection, key: str, kind: str, body: str) -> str:
    """Store (or refresh) a cached body. Returns the timestamp written."""
    fetched_at = utcnow()
    conn.execute(
        """
        INSERT INTO http_cache (cache_key, kind, body, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (cache_key) DO UPDATE SET
            kind = excluded.kind,
            body = excluded.body,
            fetched_at = excluded.fetched_at
        """,
        (key, kind, body, fetched_at),
    )
    return fetched_at


def is_expired(fetched_at: str, ttl_days: int) -> bool:
    age = datetime.now(timezone.utc) - parse_ts(fetched_at)
    return age >= timedelta(days=ttl_days)


def purge_expired(conn: sqlite3.Connection, kind: str, ttl_days: int) -> int:
    """Drop expired rows of one kind. Returns how many were removed."""
    cutoff = (parse_ts(utcnow()) - timedelta(days=ttl_days)).isoformat().replace(
        "+00:00", "Z"
    )
    cur = conn.execute(
        "DELETE FROM http_cache WHERE kind = ? AND fetched_at < ?", (kind, cutoff)
    )
    return cur.rowcount


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Row count per cache kind, for `aso` diagnostics."""
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM http_cache GROUP BY kind"
    ).fetchall()
    return {row["kind"]: row["n"] for row in rows}
