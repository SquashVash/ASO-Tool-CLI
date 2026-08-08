from __future__ import annotations

import sqlite3

import httpx
import pytest
import respx

from aso import pipeline, repository as repo
from aso.clients.hints import HintsClient
from aso.clients.itunes import ITunesClient

from .conftest import FIXTURES
from .test_hints import HINTS_URL
from .test_http import URL as ITUNES_URL, fast_fetcher

SERP_BODY = (FIXTURES / "itunes_search_candlestick_us.json").read_text(encoding="utf-8")
HINTS_BODY = (FIXTURES / "hints_candlestick_us.plist").read_text(encoding="utf-8")
EMPTY_HINTS = (FIXTURES / "hints_empty_us.plist").read_text(encoding="utf-8")


def track_keyword(conn, keyword="candlestick patterns", country="us", tags=None):
    repo.add_keyword(conn, keyword, country, tags)
    return repo.require_keyword(conn, keyword, country)


def mock_both(*, serp=200, hints=200) -> None:
    respx.get(ITUNES_URL).mock(
        return_value=httpx.Response(serp, text=SERP_BODY if serp == 200 else "nope")
    )
    respx.get(HINTS_URL).mock(
        return_value=httpx.Response(hints, text=HINTS_BODY if hints == 200 else "nope")
    )


async def run(conn, rows, **kwargs):
    async with fast_fetcher(retry_attempts=2) as fetcher:
        return await pipeline.refresh(conn, rows, fetcher=fetcher, **kwargs)


# --- happy path ------------------------------------------------------------


@respx.mock
async def test_refresh_writes_a_complete_snapshot(conn: sqlite3.Connection) -> None:
    mock_both()
    row = track_keyword(conn)
    report = await run(conn, [row])

    assert report.succeeded == 1
    assert report.failed == 0

    snapshot = repo.latest_snapshot(conn, row["id"])
    assert snapshot is not None
    assert snapshot["fetch_failed"] == 0
    assert snapshot["search_score"] is not None
    assert snapshot["competition_score"] is not None
    assert snapshot["opportunity_score"] is not None


@respx.mock
async def test_every_component_is_persisted_for_reproducibility(conn: sqlite3.Connection) -> None:
    """No score in the database may be a bare number."""
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])

    snapshot = repo.latest_snapshot(conn, row["id"])
    for name in ("comp_rating_count", "comp_exact_match", "comp_stars",
                 "comp_recency", "comp_publisher", "comp_breadth"):
        assert snapshot[name] is not None, name
    assert snapshot["search_prefix_depth"] is not None
    assert snapshot["search_hint_rank"] is not None


@respx.mock
async def test_stored_components_recompute_the_stored_score(conn: sqlite3.Connection) -> None:
    from aso.scoring.competition import combine
    from aso.scoring.opportunity import opportunity
    from aso.scoring.search import score_from_observations

    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    snapshot = repo.latest_snapshot(conn, row["id"])

    recomputed_comp = combine({k: snapshot[k] for k in [
        "comp_rating_count", "comp_exact_match", "comp_stars",
        "comp_recency", "comp_publisher", "comp_breadth",
    ]})
    recomputed_search = score_from_observations(
        snapshot["search_prefix_depth"], snapshot["search_hint_rank"], len(row["keyword"])
    )
    assert recomputed_comp == pytest.approx(snapshot["competition_score"])
    assert recomputed_search == pytest.approx(snapshot["search_score"])
    assert opportunity(recomputed_search, recomputed_comp) == pytest.approx(
        snapshot["opportunity_score"]
    )


@respx.mock
async def test_refresh_stores_the_ranking(conn: sqlite3.Connection) -> None:
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])

    serp = repo.latest_serp(conn, row["id"], limit=10)
    assert len(serp) == 10
    assert [entry["rank"] for entry in serp] == list(range(1, 11))


@respx.mock
async def test_rerunning_against_cached_responses_does_not_duplicate_the_serp(
    conn: sqlite3.Connection,
) -> None:
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    await run(conn, [row])

    captures = conn.execute(
        "SELECT COUNT(DISTINCT captured_at) FROM serps WHERE keyword_id = ?", (row["id"],)
    ).fetchone()[0]
    assert captures == 1, "the cached SERP keeps its original capture time"
    assert conn.execute("SELECT COUNT(*) FROM serps").fetchone()[0] == 43
    # ...but each run still records its own snapshot, so trends accumulate.
    assert len(repo.snapshot_history(conn, row["id"])) == 2


# --- failure handling ------------------------------------------------------


@respx.mock
async def test_serp_failure_is_recorded_not_raised(conn: sqlite3.Connection) -> None:
    mock_both(serp=403)
    row = track_keyword(conn)
    report = await run(conn, [row])

    assert report.failed == 1
    snapshot = repo.latest_snapshot(conn, row["id"])
    assert snapshot["fetch_failed"] == 1
    assert "serp" in snapshot["fetch_error"]
    # The search side still succeeded and is stored.
    assert snapshot["search_score"] is not None
    assert snapshot["competition_score"] is None


@respx.mock
async def test_hints_failure_leaves_search_null_never_zero(conn: sqlite3.Connection) -> None:
    """A failed fetch must not read as 'this keyword has no volume'."""
    mock_both(hints=403)
    row = track_keyword(conn)
    report = await run(conn, [row])

    assert report.outcomes[0].status == "partial"
    snapshot = repo.latest_snapshot(conn, row["id"])
    assert snapshot["search_score"] is None
    assert snapshot["competition_score"] is not None
    assert "hints" in snapshot["fetch_error"]


@respx.mock
async def test_partial_results_never_produce_an_opportunity_score(
    conn: sqlite3.Connection,
) -> None:
    """A half-measured keyword must not outrank a fully measured one."""
    mock_both(hints=403)
    row = track_keyword(conn)
    await run(conn, [row])
    assert repo.latest_snapshot(conn, row["id"])["opportunity_score"] is None


@respx.mock
async def test_total_failure_still_writes_a_snapshot(conn: sqlite3.Connection) -> None:
    mock_both(serp=403, hints=403)
    row = track_keyword(conn)
    report = await run(conn, [row])

    snapshot = repo.latest_snapshot(conn, row["id"])
    assert snapshot is not None, "a failed keyword still gets a row"
    assert snapshot["fetch_failed"] == 1
    assert snapshot["search_score"] is None
    assert snapshot["competition_score"] is None
    assert report.outcomes[0].status == "failed"


@respx.mock
async def test_one_failure_does_not_abort_the_run(conn: sqlite3.Connection) -> None:
    """A 500-keyword refresh must not die on keyword 300."""
    respx.get(HINTS_URL).mock(return_value=httpx.Response(200, text=HINTS_BODY))
    respx.get(ITUNES_URL).mock(
        side_effect=[
            httpx.Response(200, text=SERP_BODY),
            httpx.Response(403), httpx.Response(403),  # second keyword, both attempts
            httpx.Response(200, text=SERP_BODY),
        ]
    )
    rows = [
        track_keyword(conn, "first"),
        track_keyword(conn, "second"),
        track_keyword(conn, "third"),
    ]
    report = await run(conn, rows)

    assert len(report.outcomes) == 3
    assert report.succeeded == 2
    assert report.failed == 1
    assert all(repo.latest_snapshot(conn, row["id"]) is not None for row in rows)


@respx.mock
async def test_unknown_storefront_fails_only_that_keyword(conn: sqlite3.Connection) -> None:
    mock_both()
    row = track_keyword(conn, "forex", country="zz")
    report = await run(conn, [row])

    snapshot = repo.latest_snapshot(conn, row["id"])
    assert snapshot["fetch_failed"] == 1
    assert "storefront" in snapshot["fetch_error"].lower()
    assert snapshot["competition_score"] is not None, "the SERP side still worked"


@respx.mock
async def test_a_keyword_with_no_suggestions_scores_the_floor(conn: sqlite3.Connection) -> None:
    """No suggestions is a real answer, and must not be recorded as a failure."""
    from aso.config import SEARCH_NO_MATCH_SCORE

    respx.get(ITUNES_URL).mock(return_value=httpx.Response(200, text=SERP_BODY))
    respx.get(HINTS_URL).mock(return_value=httpx.Response(200, text=EMPTY_HINTS))
    row = track_keyword(conn, "zzqxwvj")
    report = await run(conn, [row])

    assert report.failed == 0
    snapshot = repo.latest_snapshot(conn, row["id"])
    assert snapshot["search_score"] == pytest.approx(SEARCH_NO_MATCH_SCORE)
    assert snapshot["search_prefix_depth"] is None


# --- run mechanics ---------------------------------------------------------


@respx.mock
async def test_both_clients_share_one_rate_limiter(conn: sqlite3.Connection) -> None:
    mock_both()
    rows = [track_keyword(conn)]
    async with fast_fetcher() as fetcher:
        report = await pipeline.refresh(conn, rows, fetcher=fetcher)
    assert report.requests_made == fetcher.requests_made > 1


@respx.mock
async def test_progress_callback_fires_per_keyword(conn: sqlite3.Connection) -> None:
    mock_both()
    rows = [track_keyword(conn, "a"), track_keyword(conn, "b")]
    seen: list[str] = []
    await run(conn, rows, on_progress=lambda outcome: seen.append(outcome.keyword))
    assert seen == ["a", "b"]


@respx.mock
async def test_second_run_is_served_entirely_from_cache(conn: sqlite3.Connection) -> None:
    """What makes an interrupted run cheap to resume."""
    mock_both()
    row = track_keyword(conn)
    async with fast_fetcher() as first:
        await pipeline.refresh(conn, [row], fetcher=first)
        cold = first.requests_made
    async with fast_fetcher() as second:
        await pipeline.refresh(conn, [row], fetcher=second)
        assert second.requests_made == 0
    assert cold > 0


@respx.mock
async def test_force_refetches_everything(conn: sqlite3.Connection) -> None:
    mock_both()
    row = track_keyword(conn)
    async with fast_fetcher() as first:
        await pipeline.refresh(conn, [row], fetcher=first)
        cold = first.requests_made
    async with fast_fetcher() as second:
        await pipeline.refresh(conn, [row], fetcher=second, force=True)
        assert second.requests_made == cold


@respx.mock
async def test_empty_keyword_list_is_a_no_op(conn: sqlite3.Connection) -> None:
    report = await run(conn, [])
    assert report.outcomes == []
    assert report.succeeded == 0


@respx.mock
async def test_report_counts_requests_and_duration(conn: sqlite3.Connection) -> None:
    mock_both()
    report = await run(conn, [track_keyword(conn)])
    assert report.requests_made > 0
    assert report.started_at and report.finished_at
    assert report.duration_seconds >= 0.0


@respx.mock
async def test_refresh_keyword_is_usable_on_its_own(conn: sqlite3.Connection) -> None:
    mock_both()
    row = track_keyword(conn)
    async with fast_fetcher() as fetcher:
        outcome = await pipeline.refresh_keyword(
            row["id"], row["keyword"], row["country"],
            conn=conn,
            itunes=ITunesClient(fetcher, conn),
            hints=HintsClient(fetcher, conn),
        )
    assert outcome.status == "ok"
    assert outcome.serp_size == 43
    assert outcome.ladder_queries > 0
