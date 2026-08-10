from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aso import cache


def stamp(days_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.fixture
def store_cache() -> cache.Cache:
    """A fresh cache per test.

    The production one is a module-level singleton, so tests that reached for
    it directly would leak entries into each other. `conftest` clears it too,
    but taking an explicit instance is what makes these tests order-independent
    rather than order-independent-by-luck.
    """
    return cache.Cache()


def seed(target: cache.Cache, key: str, kind: str, body: str, age_days: float) -> None:
    """Plant an entry with a chosen age.

    Reaches into `_entries` on purpose: `put` always stamps *now*, and the TTL
    behaviour under test is precisely what happens when it did not.
    """
    target._entries[key] = (kind, body, stamp(age_days))


def test_put_then_get_round_trips(store_cache: cache.Cache) -> None:
    store_cache.put("serp:us:50:forex", "serp", '{"resultCount": 1}')
    hit = store_cache.get("serp:us:50:forex", ttl_days=3)
    assert hit is not None
    assert hit.body == '{"resultCount": 1}'


def test_miss_returns_none(store_cache: cache.Cache) -> None:
    assert store_cache.get("serp:us:50:nothing", ttl_days=3) is None


def test_entry_older_than_ttl_is_a_miss(store_cache: cache.Cache) -> None:
    seed(store_cache, "serp:us:50:old", "serp", "{}", 4)
    assert store_cache.get("serp:us:50:old", ttl_days=3) is None
    # ...but the entry survives, so a failed refetch can still fall back to it.
    assert store_cache.stats() == {"serp": 1}


def test_entry_inside_ttl_is_a_hit(store_cache: cache.Cache) -> None:
    seed(store_cache, "serp:us:50:fresh", "serp", "{}", 2)
    assert store_cache.get("serp:us:50:fresh", ttl_days=3) is not None


def test_put_overwrites_and_refreshes_the_timestamp(store_cache: cache.Cache) -> None:
    seed(store_cache, "k", "serp", "old", 10)
    store_cache.put("k", "serp", "new")
    hit = store_cache.get("k", ttl_days=3)
    assert hit is not None and hit.body == "new"
    assert store_cache.stats() == {"serp": 1}


def test_purge_expired_only_touches_its_own_kind(store_cache: cache.Cache) -> None:
    seed(store_cache, "a", "serp", "{}", 10)
    seed(store_cache, "b", "serp", "{}", 1)
    seed(store_cache, "c", "hints", "<plist/>", 10)

    assert store_cache.purge_expired("serp", ttl_days=3) == 1
    assert store_cache.stats() == {"serp": 1, "hints": 1}


def test_a_new_cache_starts_empty() -> None:
    """The documented cost of moving the cache in-process.

    A second CLI run re-fetches what the first one fetched. This is the trade
    that removed 459MB of stale SERP bodies from the database, and it is worth
    a test so it reads as a decision rather than a regression.
    """
    first = cache.Cache()
    first.put("serp:us:50:forex", "serp", "{}")
    assert cache.Cache().get("serp:us:50:forex", ttl_days=3) is None


def test_keys_are_storefront_and_case_stable() -> None:
    assert cache.serp_key("Forex ", "US", 50) == cache.serp_key("forex", "us", 50)
    assert cache.serp_key("forex", "us", 50) != cache.serp_key("forex", "de", 50)
    assert cache.serp_key("forex", "us", 50) != cache.serp_key("forex", "us", 25)
    assert cache.hints_key("fo", "us") != cache.hints_key("fo", "gb")
