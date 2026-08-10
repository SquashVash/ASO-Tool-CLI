from __future__ import annotations

import sqlite3

import pytest

from aso import repository as repo
from aso.db import utcnow


def add(conn, keyword="day trading", country="us", tags=None):
    keyword_id, _ = repo.add_keyword(conn, keyword, country, tags)
    return keyword_id


def snap(conn, keyword_id, *, captured_at=None, search=50.0, comp=20.0, opp=40.0, **kw):
    repo.write_snapshot(
        conn,
        repo.SnapshotWrite(
            keyword_id=keyword_id,
            captured_at=captured_at or utcnow(),
            search_score=search,
            competition_score=comp,
            opportunity_score=opp,
            **kw,
        ),
    )


# --- tags ------------------------------------------------------------------


def test_tags_are_normalized_and_stable() -> None:
    assert repo.normalize_tags([" LCP ", "forex", "lcp"]) == "forex,lcp"
    assert repo.normalize_tags("b,a") == repo.normalize_tags(["A", "B"])
    assert repo.normalize_tags(None) == ""


def test_split_tags_drops_empties() -> None:
    assert repo.split_tags("") == []
    assert repo.split_tags("a,b") == ["a", "b"]


# --- keywords --------------------------------------------------------------


def test_add_then_read_back(conn: sqlite3.Connection) -> None:
    keyword_id, created = repo.add_keyword(conn, "  Day  Trading ", "US", ["LCP"])
    assert created is True
    row = repo.get_keyword(conn, "day trading", "us")
    assert row is not None
    assert row["id"] == keyword_id
    assert row["keyword"] == "day trading", "whitespace collapsed, lowercased"
    assert row["country"] == "us"
    assert row["tags"] == "lcp"


def test_case_variants_are_one_keyword_not_two(conn: sqlite3.Connection) -> None:
    """App Store search is case-insensitive; tracking both would split history."""
    first, created_first = repo.add_keyword(conn, "Day Trading", "us")
    second, created_second = repo.add_keyword(conn, "day trading", "us")
    assert (created_first, created_second) == (True, False)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 1


def test_readding_merges_tags_instead_of_replacing(conn: sqlite3.Connection) -> None:
    """Re-importing an overlapping CSV must be safe."""
    repo.add_keyword(conn, "day trading", "us", ["lcp"])
    keyword_id, created = repo.add_keyword(conn, "day trading", "us", ["forex"])
    assert created is False
    assert repo.get_keyword(conn, "day trading", "us")["tags"] == "forex,lcp"
    assert conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 1
    assert keyword_id


def test_same_keyword_in_two_storefronts_is_two_rows(conn: sqlite3.Connection) -> None:
    first = add(conn, country="us")
    second = add(conn, country="de")
    assert first != second


def test_empty_keyword_or_country_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        repo.add_keyword(conn, "   ", "us")
    with pytest.raises(ValueError):
        repo.add_keyword(conn, "forex", "  ")


def test_require_keyword_raises_for_unknown(conn: sqlite3.Connection) -> None:
    with pytest.raises(repo.UnknownKeyword):
        repo.require_keyword(conn, "nope", "us")


def test_list_filters_by_tag_country_and_active(conn: sqlite3.Connection) -> None:
    add(conn, "a", "us", ["lcp"])
    add(conn, "b", "us", ["forex"])
    add(conn, "c", "de", ["lcp"])
    inactive = add(conn, "d", "us", ["lcp"])
    repo.set_active(conn, inactive, False)

    assert [r["keyword"] for r in repo.list_keywords(conn, tag="lcp")] == ["a", "c"]
    assert [r["keyword"] for r in repo.list_keywords(conn, country="us")] == ["a", "b"]
    assert [r["keyword"] for r in repo.list_keywords(conn, tag="lcp", country="us")] == ["a"]
    assert len(repo.list_keywords(conn, tag="lcp", active_only=False)) == 3


def test_tag_filter_does_not_match_a_prefix(conn: sqlite3.Connection) -> None:
    """'lcp' must not match the keyword tagged 'lcp-old'."""
    add(conn, "a", "us", ["lcp-old"])
    add(conn, "b", "us", ["lcp"])
    assert [r["keyword"] for r in repo.list_keywords(conn, tag="lcp")] == ["b"]


def test_all_tags_and_countries(conn: sqlite3.Connection) -> None:
    add(conn, "a", "us", ["lcp", "forex"])
    add(conn, "b", "de", ["lcp"])
    assert repo.all_tags(conn) == ["forex", "lcp"]
    assert repo.countries(conn) == ["de", "us"]


# --- snapshots -------------------------------------------------------------


def test_latest_snapshot_is_the_newest(conn: sqlite3.Connection) -> None:
    keyword_id = add(conn)
    snap(conn, keyword_id, captured_at="2026-01-01T00:00:00Z", opp=10.0)
    snap(conn, keyword_id, captured_at="2026-06-01T00:00:00Z", opp=90.0)
    assert repo.latest_snapshot(conn, keyword_id)["opportunity_score"] == 90.0


def test_history_is_chronological(conn: sqlite3.Connection) -> None:
    keyword_id = add(conn)
    for stamp in ("2026-03-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"):
        snap(conn, keyword_id, captured_at=stamp)
    stamps = [r["captured_at"] for r in repo.snapshot_history(conn, keyword_id)]
    assert stamps == sorted(stamps)


def test_history_limit_keeps_the_newest_but_stays_chronological(conn: sqlite3.Connection) -> None:
    keyword_id = add(conn)
    for month in range(1, 6):
        snap(conn, keyword_id, captured_at=f"2026-0{month}-01T00:00:00Z")
    stamps = [r["captured_at"] for r in repo.snapshot_history(conn, keyword_id, limit=2)]
    assert stamps == ["2026-04-01T00:00:00Z", "2026-05-01T00:00:00Z"]


def test_snapshot_stores_every_component(conn: sqlite3.Connection) -> None:
    """The reproducibility promise, at the storage layer."""
    keyword_id = add(conn)
    snap(
        conn, keyword_id,
        comp_rating_count=11.0, comp_exact_match=22.0, comp_stars=33.0,
        comp_recency=44.0, comp_publisher=55.0, comp_breadth=66.0,
        search_prefix_depth=4, search_hint_rank=2,
    )
    row = repo.latest_snapshot(conn, keyword_id)
    assert row["comp_rating_count"] == 11.0
    assert row["comp_breadth"] == 66.0
    assert row["search_prefix_depth"] == 4
    assert row["search_hint_rank"] == 2


# --- latest_scores ---------------------------------------------------------


def test_latest_scores_sorts_by_opportunity_descending(conn: sqlite3.Connection) -> None:
    low, high = add(conn, "low"), add(conn, "high")
    snap(conn, low, opp=10.0)
    snap(conn, high, opp=90.0)
    assert [r["keyword"] for r in repo.latest_scores(conn)] == ["high", "low"]


def test_unscored_keywords_sort_last_never_first(conn: sqlite3.Connection) -> None:
    """An unmeasured keyword is not a high-opportunity one."""
    scored = add(conn, "scored")
    add(conn, "never refreshed")
    snap(conn, scored, opp=5.0)
    rows = repo.latest_scores(conn)
    assert [r["keyword"] for r in rows] == ["scored", "never refreshed"]
    assert rows[1]["opportunity_score"] is None


def test_unscored_can_be_excluded(conn: sqlite3.Connection) -> None:
    scored = add(conn, "scored")
    add(conn, "unscored")
    snap(conn, scored)
    rows = repo.latest_scores(conn, include_unscored=False)
    assert [r["keyword"] for r in rows] == ["scored"]


def test_latest_scores_uses_only_the_newest_snapshot(conn: sqlite3.Connection) -> None:
    keyword_id = add(conn)
    snap(conn, keyword_id, captured_at="2026-01-01T00:00:00Z", opp=99.0)
    snap(conn, keyword_id, captured_at="2026-06-01T00:00:00Z", opp=11.0)
    rows = repo.latest_scores(conn)
    assert len(rows) == 1
    assert rows[0]["opportunity_score"] == 11.0


@pytest.mark.parametrize("sort", sorted(repo.SORT_COLUMNS))
def test_every_advertised_sort_works(conn: sqlite3.Connection, sort: str) -> None:
    keyword_id = add(conn)
    snap(conn, keyword_id)
    assert len(repo.latest_scores(conn, sort=sort)) == 1


def test_unknown_sort_is_rejected(conn: sqlite3.Connection) -> None:
    """Sort keys are interpolated into SQL, so the allowlist matters."""
    with pytest.raises(ValueError):
        repo.latest_scores(conn, sort="opportunity; DROP TABLE keywords")


def test_latest_scores_respects_filters_and_limit(conn: sqlite3.Connection) -> None:
    for index, (kw, cc, tags) in enumerate(
        [("a", "us", ["lcp"]), ("b", "us", ["lcp"]), ("c", "de", ["lcp"])]
    ):
        snap(conn, add(conn, kw, cc, tags), opp=float(index))
    assert len(repo.latest_scores(conn, country="us")) == 2
    assert len(repo.latest_scores(conn, tag="lcp", limit=2)) == 2


# --- serps -----------------------------------------------------------------


def test_write_serp_stores_rank_order(conn: sqlite3.Connection) -> None:
    keyword_id = add(conn)
    repo.write_serp(conn, keyword_id, "2026-06-01T00:00:00Z", [111, 222, 333])
    rows = repo.latest_serp(conn, keyword_id)
    assert [(r["rank"], r["track_id"]) for r in rows] == [(1, 111), (2, 222), (3, 333)]


def test_rewriting_the_same_capture_replaces_it(conn: sqlite3.Connection) -> None:
    """Re-running a refresh against a cached SERP must not duplicate rows."""
    keyword_id = add(conn)
    repo.write_serp(conn, keyword_id, "2026-06-01T00:00:00Z", [111, 222])
    repo.write_serp(conn, keyword_id, "2026-06-01T00:00:00Z", [333])
    rows = repo.latest_serp(conn, keyword_id)
    assert [r["track_id"] for r in rows] == [333]


def test_latest_serp_joins_app_metadata_for_the_right_storefront(conn: sqlite3.Connection) -> None:
    keyword_id = add(conn, country="de")
    conn.execute(
        "INSERT INTO apps (track_id, country, track_name, fetched_at) VALUES (?,?,?,?)",
        (111, "de", "Deutsche App", utcnow()),
    )
    conn.execute(
        "INSERT INTO apps (track_id, country, track_name, fetched_at) VALUES (?,?,?,?)",
        (111, "us", "US App", utcnow()),
    )
    repo.write_serp(conn, keyword_id, "2026-06-01T00:00:00Z", [111])
    assert repo.latest_serp(conn, keyword_id)[0]["track_name"] == "Deutsche App"


def test_latest_serp_survives_missing_app_metadata(conn: sqlite3.Connection) -> None:
    keyword_id = add(conn)
    repo.write_serp(conn, keyword_id, "2026-06-01T00:00:00Z", [999])
    row = repo.latest_serp(conn, keyword_id)[0]
    assert row["track_id"] == 999
    assert row["track_name"] is None


def test_latest_serp_shows_only_the_newest_capture(conn: sqlite3.Connection) -> None:
    keyword_id = add(conn)
    repo.write_serp(conn, keyword_id, "2026-01-01T00:00:00Z", [111])
    repo.write_serp(conn, keyword_id, "2026-06-01T00:00:00Z", [222, 333])
    assert [r["track_id"] for r in repo.latest_serp(conn, keyword_id)] == [222, 333]


# --- track -----------------------------------------------------------------


def test_track_reports_current_and_previous_rank(conn: sqlite3.Connection) -> None:
    keyword_id = add(conn)
    repo.write_serp(conn, keyword_id, "2026-01-01T00:00:00Z", [999, 111])
    repo.write_serp(conn, keyword_id, "2026-06-01T00:00:00Z", [111, 999])
    row = repo.track_positions(conn, 111)[0]
    assert row["rank"] == 1
    assert row["previous_rank"] == 2


def test_track_shows_an_app_that_dropped_out(conn: sqlite3.Connection) -> None:
    """Falling out of the ranking is the movement most worth seeing."""
    keyword_id = add(conn)
    repo.write_serp(conn, keyword_id, "2026-01-01T00:00:00Z", [111])
    repo.write_serp(conn, keyword_id, "2026-06-01T00:00:00Z", [222])
    row = repo.track_positions(conn, 111)[0]
    assert row["rank"] is None
    assert row["previous_rank"] == 1


def test_track_marks_a_new_entry(conn: sqlite3.Connection) -> None:
    keyword_id = add(conn)
    repo.write_serp(conn, keyword_id, "2026-01-01T00:00:00Z", [222])
    repo.write_serp(conn, keyword_id, "2026-06-01T00:00:00Z", [111])
    row = repo.track_positions(conn, 111)[0]
    assert row["rank"] == 1
    assert row["previous_rank"] is None


def test_track_ignores_keywords_the_app_never_appeared_in(conn: sqlite3.Connection) -> None:
    other = add(conn, "unrelated")
    repo.write_serp(conn, other, "2026-06-01T00:00:00Z", [222])
    assert repo.track_positions(conn, 111) == []


def test_track_can_be_scoped_to_a_storefront(conn: sqlite3.Connection) -> None:
    us = add(conn, "kw", "us")
    de = add(conn, "kw", "de")
    for keyword_id in (us, de):
        repo.write_serp(conn, keyword_id, "2026-06-01T00:00:00Z", [111])
    assert len(repo.track_positions(conn, 111)) == 2
    assert len(repo.track_positions(conn, 111, country="de")) == 1


# --- movers ----------------------------------------------------------------


def mover_snap(conn, keyword_id, captured_at, opportunity, search=50.0, competition=50.0):
    return repo.write_snapshot(
        conn,
        repo.SnapshotWrite(
            keyword_id=keyword_id,
            captured_at=captured_at,
            search_score=search,
            competition_score=competition,
            opportunity_score=opportunity,
        ),
    )


def days_ago(days: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_movers_reports_the_change_since_the_cutoff(conn) -> None:
    repo.add_keyword(conn, "forex", "us")
    row = repo.require_keyword(conn, "forex", "us")
    mover_snap(conn, row["id"], days_ago(10), 20.0)
    mover_snap(conn, row["id"], days_ago(0), 35.0)

    movers = repo.score_movers(conn, days=7)
    assert len(movers) == 1
    assert movers[0]["opportunity_score"] == pytest.approx(35.0)
    assert movers[0]["baseline_opportunity"] == pytest.approx(20.0)
    assert movers[0]["opportunity_delta"] == pytest.approx(15.0)


def test_movers_baseline_is_the_cutoff_not_merely_the_previous_snapshot(conn) -> None:
    """Two refreshes in one afternoon must not read as a week-over-week move."""
    repo.add_keyword(conn, "forex", "us")
    row = repo.require_keyword(conn, "forex", "us")
    mover_snap(conn, row["id"], days_ago(30), 10.0)
    mover_snap(conn, row["id"], days_ago(0), 40.0)
    mover_snap(conn, row["id"], days_ago(0), 41.0)

    movers = repo.score_movers(conn, days=7)
    assert movers[0]["baseline_opportunity"] == pytest.approx(10.0), (
        "the baseline must be the snapshot older than the cutoff"
    )
    assert movers[0]["opportunity_delta"] == pytest.approx(31.0)


def test_a_keyword_with_no_old_enough_snapshot_has_a_null_delta(conn) -> None:
    """Zero would claim 'measured, didn't move'. This is 'not measured then'."""
    repo.add_keyword(conn, "forex", "us")
    row = repo.require_keyword(conn, "forex", "us")
    mover_snap(conn, row["id"], days_ago(1), 40.0)

    movers = repo.score_movers(conn, days=7)
    assert movers[0]["opportunity_delta"] is None
    assert movers[0]["baseline_opportunity"] is None


def test_a_single_snapshot_older_than_the_cutoff_still_has_no_delta(conn) -> None:
    """The baseline and the latest are the same row; comparing gives nothing."""
    repo.add_keyword(conn, "forex", "us")
    row = repo.require_keyword(conn, "forex", "us")
    mover_snap(conn, row["id"], days_ago(30), 40.0)

    movers = repo.score_movers(conn, days=7)
    assert movers[0]["opportunity_delta"] is None


def test_movers_sorts_by_absolute_movement(conn) -> None:
    """A big drop is as interesting as a big rise."""
    for keyword, then, now in (("a", 20.0, 25.0), ("b", 60.0, 20.0), ("c", 30.0, 32.0)):
        repo.add_keyword(conn, keyword, "us")
        row = repo.require_keyword(conn, keyword, "us")
        mover_snap(conn, row["id"], days_ago(10), then)
        mover_snap(conn, row["id"], days_ago(0), now)

    movers = repo.score_movers(conn, days=7)
    assert [m["keyword"] for m in movers] == ["b", "a", "c"]


def test_movers_respects_country_and_tag_filters(conn) -> None:
    for keyword, country, tags in (("forex", "us", "fx"), ("forex", "de", "fx"),
                                   ("gold", "us", "metal")):
        repo.add_keyword(conn, keyword, country, tags)
        row = repo.require_keyword(conn, keyword, country)
        mover_snap(conn, row["id"], days_ago(10), 20.0)
        mover_snap(conn, row["id"], days_ago(0), 30.0)

    assert len(repo.score_movers(conn, days=7, country="us")) == 2
    assert len(repo.score_movers(conn, days=7, tag="fx")) == 2
    assert len(repo.score_movers(conn, days=7, country="us", tag="fx")) == 1


def test_movers_skips_failed_snapshots(conn) -> None:
    """A failed fetch has no opportunity score and is not a movement."""
    repo.add_keyword(conn, "forex", "us")
    row = repo.require_keyword(conn, "forex", "us")
    repo.write_snapshot(
        conn,
        repo.SnapshotWrite(
            keyword_id=row["id"], captured_at=days_ago(0),
            fetch_failed=True, fetch_error="hints: HTTP 403",
        ),
    )
    assert repo.score_movers(conn, days=7) == []


def test_movers_excludes_inactive_keywords(conn) -> None:
    repo.add_keyword(conn, "forex", "us")
    row = repo.require_keyword(conn, "forex", "us")
    mover_snap(conn, row["id"], days_ago(10), 20.0)
    mover_snap(conn, row["id"], days_ago(0), 30.0)
    repo.set_active(conn, row["id"], False)
    assert repo.score_movers(conn, days=7) == []


def test_movers_on_an_empty_database(conn) -> None:
    assert repo.score_movers(conn) == []


def test_get_keyword_by_id_round_trips(conn):
    keyword_id, _ = repo.add_keyword(conn, "forex", "us", ["lcp"])
    row = repo.get_keyword_by_id(conn, keyword_id)
    assert row["keyword"] == "forex"
    assert row["country"] == "us"


def test_get_keyword_by_id_returns_none_when_absent(conn):
    assert repo.get_keyword_by_id(conn, 9999) is None
