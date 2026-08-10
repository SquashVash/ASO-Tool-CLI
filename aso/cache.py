"""In-memory response cache, one per process.

Bodies are stored exactly as received. Reparsing is cheap; refetching costs
four seconds of rate-limit budget, so a keyword looked up twice in a session is
free the second time.

WHY THIS IS NO LONGER ON DISK
-----------------------------
It used to be an `http_cache` table, so a refresh interrupted halfway could
resume without re-paying for what it had already fetched. That resumability
cost 459MB of the 492MB database — SERP bodies, mostly, kept long past the
three-day TTL that made them meaningful, because nothing ever purged them
unless asked.

The trade is deliberate and it is not free: a CLI run that dies partway now
starts from nothing, and a chart index costs its 48 requests again. What makes
it acceptable is where the caching actually mattered. The long-lived caller is
the API server, which holds one process open all day and keeps its cache warm
across every request; the CLI's caching only ever spanned a single command.

TTLs still come from config — SERPs 3 days, autocomplete 3 days — and still
apply, because the API process outlives them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .files import parse_ts, utcnow


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


class Cache:
    """Keyed bodies with a per-read TTL.

    Guarded by a lock because the API serves requests from a threadpool and one
    process holds one cache. The critical sections are dict operations, so the
    lock is never held across I/O.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, str, str]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl_days: int) -> CachedBody | None:
        """The cached body if present and younger than `ttl_days`.

        A stale entry is left in place rather than dropped: if the refetch
        fails, the stale copy is still there for `purge_expired` to report on,
        and evicting on read would make a failed refetch worse than no refetch.
        """
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None
        _kind, body, fetched_at = entry
        if is_expired(fetched_at, ttl_days):
            return None
        return CachedBody(body=body, fetched_at=fetched_at)

    def put(self, key: str, kind: str, body: str) -> str:
        """Store (or refresh) a cached body. Returns the timestamp written."""
        fetched_at = utcnow()
        with self._lock:
            self._entries[key] = (kind, body, fetched_at)
        return fetched_at

    def purge_expired(self, kind: str, ttl_days: int) -> int:
        """Drop expired entries of one kind. Returns how many were removed."""
        with self._lock:
            stale = [
                key
                for key, (entry_kind, _body, fetched_at) in self._entries.items()
                if entry_kind == kind and is_expired(fetched_at, ttl_days)
            ]
            for key in stale:
                del self._entries[key]
        return len(stale)

    def stats(self) -> dict[str, int]:
        """Entry count per cache kind, for diagnostics."""
        counts: dict[str, int] = {}
        with self._lock:
            for kind, _body, _fetched_at in self._entries.values():
                counts[kind] = counts.get(kind, 0) + 1
        return counts

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def is_expired(fetched_at: str, ttl_days: int) -> bool:
    age = datetime.now(timezone.utc) - parse_ts(fetched_at)
    return age >= timedelta(days=ttl_days)


# The process-wide cache. Clients take a `Cache` rather than reaching for this,
# so a test can hand over an empty one instead of clearing global state.
default_cache = Cache()
