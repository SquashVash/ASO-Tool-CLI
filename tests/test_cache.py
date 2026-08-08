from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from aso import cache


def stamp(days_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_put_then_get_round_trips(conn: sqlite3.Connection) -> None:
    cache.put(conn, "serp:us:50:forex", "serp", '{"resultCount": 1}')
    hit = cache.get(conn, "serp:us:50:forex", ttl_days=3)
    assert hit is not None
    assert hit.body == '{"resultCount": 1}'


def test_miss_returns_none(conn: sqlite3.Connection) -> None:
    assert cache.get(conn, "serp:us:50:nothing", ttl_days=3) is None


def test_entry_older_than_ttl_is_a_miss(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO http_cache (cache_key, kind, body, fetched_at) VALUES (?,?,?,?)",
        ("serp:us:50:old", "serp", "{}", stamp(4)),
    )
    assert cache.get(conn, "serp:us:50:old", ttl_days=3) is None
    # ...but the row survives, so a failed refetch can still fall back to it.
    assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 1


def test_entry_inside_ttl_is_a_hit(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO http_cache (cache_key, kind, body, fetched_at) VALUES (?,?,?,?)",
        ("serp:us:50:fresh", "serp", "{}", stamp(2)),
    )
    assert cache.get(conn, "serp:us:50:fresh", ttl_days=3) is not None


def test_put_overwrites_and_refreshes_the_timestamp(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO http_cache (cache_key, kind, body, fetched_at) VALUES (?,?,?,?)",
        ("k", "serp", "old", stamp(10)),
    )
    cache.put(conn, "k", "serp", "new")
    hit = cache.get(conn, "k", ttl_days=3)
    assert hit is not None and hit.body == "new"
    assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 1


def test_purge_expired_only_touches_its_own_kind(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO http_cache (cache_key, kind, body, fetched_at) VALUES (?,?,?,?)",
        [
            ("a", "serp", "{}", stamp(10)),
            ("b", "serp", "{}", stamp(1)),
            ("c", "hints", "<plist/>", stamp(10)),
        ],
    )
    assert cache.purge_expired(conn, "serp", ttl_days=3) == 1
    assert cache.stats(conn) == {"serp": 1, "hints": 1}


def test_keys_are_storefront_and_case_stable() -> None:
    assert cache.serp_key("Forex ", "US", 50) == cache.serp_key("forex", "us", 50)
    assert cache.serp_key("forex", "us", 50) != cache.serp_key("forex", "de", 50)
    assert cache.serp_key("forex", "us", 50) != cache.serp_key("forex", "us", 25)
    assert cache.hints_key("fo", "us") != cache.hints_key("fo", "gb")
