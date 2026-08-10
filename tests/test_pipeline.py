from __future__ import annotations

import json
import math
import sqlite3

import httpx
import pytest
import respx

from aso import config, pipeline, repository as repo
from aso.clients.hints import HintsClient
from aso.clients.itunes import ITunesClient

from .conftest import FIXTURES
from .test_hints import HINTS_URL
from .test_http import URL as ITUNES_URL, fast_fetcher

SERP_BODY = (FIXTURES / "itunes_search_candlestick_us.json").read_text(encoding="utf-8")
HINTS_BODY = (FIXTURES / "hints_candlestick_us.plist").read_text(encoding="utf-8")
EMPTY_HINTS = (FIXTURES / "hints_empty_us.plist").read_text(encoding="utf-8")
CHARTS_BODY = (FIXTURES / "charts_finance_free_us.json").read_text(encoding="utf-8")

# Every chart feed on any storefront. One body for all 48 of them: what these
# tests care about is that the index is built and threaded through, not which
# genre a given app charts in.
CHARTS_URL_PATTERN = r"https://itunes\.apple\.com/[a-z]{2}/rss/.+/json"


def track_keyword(conn, keyword="candlestick patterns", country="us", tags=None):
    repo.add_keyword(conn, keyword, country, tags)
    return repo.require_keyword(conn, keyword, country)


def mock_charts(status: int = 200) -> None:
    """Serve the same chart body for every (feed, genre) URL.

    Registered before the search route because both live on itunes.apple.com
    and respx matches in registration order; the search route is an exact URL
    so it cannot swallow these, but keeping the specific pattern first makes
    that independent of respx's matching rules.
    """
    respx.get(url__regex=CHARTS_URL_PATTERN).mock(
        return_value=httpx.Response(status, text=CHARTS_BODY if status == 200 else "nope")
    )


def mock_both(*, serp=200, hints=200, charts=200) -> None:
    mock_charts(charts)
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
                 "comp_recency", "comp_publisher", "comp_breadth",
                 "comp_incumbent", "comp_app_power"):
        assert snapshot[name] is not None, name
    assert snapshot["search_prefix_depth"] is not None
    assert snapshot["search_hint_rank"] is not None


@respx.mock
async def test_stored_components_recompute_the_stored_score(conn: sqlite3.Connection) -> None:
    from aso.scoring.competition import combine, derive
    from aso.scoring.opportunity import opportunity
    from aso.scoring.search import score_from_observations

    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    snapshot = repo.latest_snapshot(conn, row["id"])

    # Deliberately rebuilt from the stored MEASUREMENTS only — comp_incumbent
    # is left out and re-derived. That is the stronger property: the score has
    # to be reproducible from what was observed, not from a derived column that
    # could have been written by a formula no longer in force.
    recomputed_comp = combine(derive({k: snapshot[k] for k in [
        "comp_rating_count", "comp_exact_match", "comp_stars",
        "comp_recency", "comp_publisher", "comp_breadth",
        "comp_app_power",
    ]}))
    recomputed_search = score_from_observations(
        snapshot["search_prefix_depth"],
        snapshot["search_hint_rank"],
        len(row["keyword"]),
        extensions=snapshot["search_hint_extensions"],
        rating_mass=snapshot["comp_rating_count"],
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
    mock_charts()
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
async def test_a_keyword_with_no_suggestions_is_not_a_failure(conn: sqlite3.Connection) -> None:
    """No suggestions is a real answer, and must not be recorded as a failure.

    It is no longer the floor score, though. Autocomplete silence used to be
    the whole verdict; now it zeroes three components out of five while the
    SERP still speaks, so the keyword scores low rather than bottomed-out.
    That is the intended behaviour — a term Apple will not complete can still
    be one that big apps compete over.
    """
    mock_charts()
    respx.get(ITUNES_URL).mock(return_value=httpx.Response(200, text=SERP_BODY))
    respx.get(HINTS_URL).mock(return_value=httpx.Response(200, text=EMPTY_HINTS))
    row = track_keyword(conn, "zzqxwvj")
    report = await run(conn, [row])

    assert report.failed == 0
    snapshot = repo.latest_snapshot(conn, row["id"])
    assert snapshot["search_prefix_depth"] is None
    assert snapshot["search_hint_rank"] is None
    assert snapshot["search_hint_extensions"] == 0, "measured, not unmeasured"
    # Only rating mass survives, so the score is its weighted share.
    assert 0 < snapshot["search_score"] < 30


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
async def test_force_refetches_everything_except_the_chart_index(
    conn: sqlite3.Connection,
) -> None:
    """`force` means "refetch this keyword", not "refetch the whole storefront".

    The chart index is a property of the country and the day, shared by every
    keyword in the run. Re-pulling it behind `force` would make a one-keyword
    `aso refresh --force` cost 48 extra requests to rebuild an index that is
    still valid, so it stays on its own daily TTL.
    """
    mock_both()
    row = track_keyword(conn)
    async with fast_fetcher() as first:
        await pipeline.refresh(conn, [row], fetcher=first)
        cold = first.requests_made
    async with fast_fetcher() as second:
        await pipeline.refresh(conn, [row], fetcher=second, force=True)
        forced = second.requests_made

    charts = len(config.CHART_FEEDS) * len(config.CHART_GENRES)
    assert cold > charts, "the cold run paid for the index"
    assert forced == cold - charts, "the forced run re-paid for everything but it"
    assert forced > 0, "the keyword's own requests were genuinely refetched"


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


# --- re-scoring stored history ---------------------------------------------


@respx.mock
async def test_rescore_reproduces_the_same_numbers_when_nothing_changed(
    conn: sqlite3.Connection,
) -> None:
    """The reproducibility promise: finals are a pure function of the row."""
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    before = dict(repo.latest_snapshot(conn, row["id"]))

    report = pipeline.rescore(conn)
    after = dict(repo.latest_snapshot(conn, row["id"]))

    assert report.total == 1
    assert report.changed == 0
    for column in ("search_score", "competition_score", "opportunity_score"):
        assert after[column] == pytest.approx(before[column])


@respx.mock
async def test_rescore_applies_new_competition_weights_to_old_snapshots(
    conn: sqlite3.Connection,
) -> None:
    import dataclasses

    from aso.config import settings as live

    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    before = repo.latest_snapshot(conn, row["id"])["competition_score"]

    # Put every ounce of weight on one component.
    skewed = dataclasses.replace(
        live,
        competition_weights={
            "comp_rating_count": 1.0, "comp_exact_match": 0.0, "comp_stars": 0.0,
            "comp_recency": 0.0, "comp_publisher": 0.0, "comp_breadth": 0.0,
        },
    )
    report = pipeline.rescore(conn, config=skewed)

    after = repo.latest_snapshot(conn, row["id"])
    assert report.changed == 1
    assert after["competition_score"] != pytest.approx(before)
    assert after["competition_score"] == pytest.approx(after["comp_rating_count"])


@respx.mock
async def test_rescore_fills_comp_incumbent_on_pre_migration_snapshots(
    conn: sqlite3.Connection,
) -> None:
    """The documented upgrade path for db.py migration 10.

    The migration adds the column but cannot backfill it — SQLite's sqrt() is
    not present in every build — so `aso rescore` is what populates history.
    Simulated here by nulling the column on a row that has its two inputs.
    """
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    snapshot_id = repo.latest_snapshot(conn, row["id"])["id"]
    conn.execute(
        "UPDATE snapshots SET comp_incumbent = NULL WHERE id = ?", (snapshot_id,)
    )
    conn.commit()

    pipeline.rescore(conn)

    after = repo.latest_snapshot(conn, row["id"])
    assert after["comp_incumbent"] is not None
    assert after["comp_incumbent"] == pytest.approx(
        math.sqrt(after["comp_rating_count"] * after["comp_stars"])
    )


@respx.mock
async def test_rescore_recomputes_comp_incumbent_rather_than_trusting_it(
    conn: sqlite3.Connection,
) -> None:
    """A stored derived column must never outrank the formula that defines it."""
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    snapshot_id = repo.latest_snapshot(conn, row["id"])["id"]
    conn.execute(
        "UPDATE snapshots SET comp_incumbent = 99.0 WHERE id = ?", (snapshot_id,)
    )
    conn.commit()

    pipeline.rescore(conn)

    after = repo.latest_snapshot(conn, row["id"])
    assert after["comp_incumbent"] == pytest.approx(
        math.sqrt(after["comp_rating_count"] * after["comp_stars"])
    )
    assert after["comp_incumbent"] != pytest.approx(99.0)


@respx.mock
async def test_rescore_leaves_components_and_observations_untouched(
    conn: sqlite3.Connection,
) -> None:
    """Inputs are inputs. A re-score must never rewrite what was measured."""
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    before = dict(repo.latest_snapshot(conn, row["id"]))

    pipeline.rescore(conn)
    after = dict(repo.latest_snapshot(conn, row["id"]))

    for column in ("comp_rating_count", "comp_exact_match", "comp_stars",
                   "comp_recency", "comp_publisher", "comp_breadth",
                   "search_prefix_depth", "search_hint_rank", "captured_at"):
        assert after[column] == before[column]


@respx.mock
async def test_rescore_does_not_turn_a_failed_fetch_into_a_measurement(
    conn: sqlite3.Connection,
) -> None:
    """A hints failure has no observations; it must stay NULL, not become the floor."""
    mock_both(hints=403)
    row = track_keyword(conn)
    await run(conn, [row])
    assert repo.latest_snapshot(conn, row["id"])["search_score"] is None

    pipeline.rescore(conn)
    after = repo.latest_snapshot(conn, row["id"])
    assert after["search_score"] is None
    assert after["opportunity_score"] is None
    assert after["fetch_failed"] == 1


@respx.mock
async def test_rescore_keeps_a_genuine_no_match_at_the_floor(
    conn: sqlite3.Connection,
) -> None:
    """Measured-and-absent is different from never-measured, and survives a re-score."""
    mock_charts()
    respx.get(ITUNES_URL).mock(return_value=httpx.Response(200, text=SERP_BODY))
    respx.get(HINTS_URL).mock(return_value=httpx.Response(200, text=EMPTY_HINTS))
    row = track_keyword(conn)
    await run(conn, [row])
    floor = repo.latest_snapshot(conn, row["id"])["search_score"]
    assert floor is not None

    pipeline.rescore(conn)
    assert repo.latest_snapshot(conn, row["id"])["search_score"] == pytest.approx(floor)


@respx.mock
async def test_rescore_applies_a_stored_competition_bridge(
    conn: sqlite3.Connection,
) -> None:
    """The level fix, end to end: raw stays raw, the final gets lifted."""
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])

    before = repo.latest_snapshot(conn, row["id"])
    raw = before["competition_score_raw"]
    assert raw is not None
    assert before["competition_score"] == pytest.approx(raw), "no bridge yet"

    # A bridge that maps everything upward, which is the reported symptom.
    repo.write_bridge(
        conn,
        country="us",
        source=config.COMPETITION_BRIDGE_SOURCE,
        knots_json=json.dumps([[0.0, 20.0], [100.0, 100.0]]),
        n_overlap=50,
        rmse=1.0,
        metric="competition",
    )
    pipeline.rescore(conn)

    after = repo.latest_snapshot(conn, row["id"])
    assert after["competition_score_raw"] == pytest.approx(raw), "raw is untouched"
    assert after["competition_score"] > raw, "the final was lifted onto the scale"


@respx.mock
async def test_a_bridged_score_is_never_bridged_twice(
    conn: sqlite3.Connection,
) -> None:
    """The whole reason `competition_score_raw` is its own column.

    `rescore` is run routinely. If it re-bridged the already-bridged value the
    score would climb a little further every time, plausibly, and forever.
    """
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    repo.write_bridge(
        conn,
        country="us",
        source=config.COMPETITION_BRIDGE_SOURCE,
        knots_json=json.dumps([[0.0, 20.0], [100.0, 100.0]]),
        n_overlap=50,
        rmse=1.0,
        metric="competition",
    )

    pipeline.rescore(conn)
    once = repo.latest_snapshot(conn, row["id"])["competition_score"]
    pipeline.rescore(conn)
    twice = repo.latest_snapshot(conn, row["id"])["competition_score"]
    assert once == pytest.approx(twice)


@respx.mock
async def test_a_demand_bridge_is_not_mistaken_for_a_competition_one(
    conn: sqlite3.Connection,
) -> None:
    """What the `metric` column exists to prevent."""
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    raw = repo.latest_snapshot(conn, row["id"])["competition_score_raw"]

    repo.write_bridge(
        conn,
        country="us",
        source="apple",
        knots_json=json.dumps([[0.0, 90.0], [100.0, 100.0]]),
        n_overlap=50,
        rmse=1.0,
    )
    pipeline.rescore(conn)

    after = repo.latest_snapshot(conn, row["id"])
    assert after["competition_score"] == pytest.approx(raw), "demand bridge leaked"


def test_rescore_of_an_empty_database_is_a_no_op(conn: sqlite3.Connection) -> None:
    report = pipeline.rescore(conn)
    assert report.total == 0
    assert report.changed == 0


# --- extensions backfill ---------------------------------------------------


@respx.mock
async def test_rescore_backfills_extensions_from_the_cache(
    conn: sqlite3.Connection,
) -> None:
    """History written before the column existed is recoverable without Apple.

    Every ladder walk's first rung is the keyword's own full-length prefix, so
    the suggestion list the count comes from is already in `http_cache`.
    """
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])

    conn.execute("UPDATE snapshots SET search_hint_extensions = NULL")
    conn.commit()

    report = pipeline.rescore(conn)

    assert report.backfilled_extensions == 1
    restored = repo.latest_snapshot(conn, row["id"])["search_hint_extensions"]
    assert restored is not None


@respx.mock
async def test_backfill_never_overwrites_a_measured_count(
    conn: sqlite3.Connection,
) -> None:
    """A stored count is a measurement. Re-deriving it is not an improvement."""
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    conn.execute("UPDATE snapshots SET search_hint_extensions = 7")
    conn.commit()

    assert pipeline.backfill_extensions(conn) == 0
    assert repo.latest_snapshot(conn, row["id"])["search_hint_extensions"] == 7


@respx.mock
async def test_backfill_leaves_null_when_the_cache_is_gone(
    conn: sqlite3.Connection,
) -> None:
    """NULL means "nobody looked". Writing 0 would claim Apple offered nothing."""
    mock_both()
    row = track_keyword(conn)
    await run(conn, [row])
    conn.execute("UPDATE snapshots SET search_hint_extensions = NULL")
    conn.execute("DELETE FROM http_cache WHERE kind = 'hints'")
    conn.commit()

    assert pipeline.backfill_extensions(conn) == 0
    assert repo.latest_snapshot(conn, row["id"])["search_hint_extensions"] is None


@respx.mock
async def test_calibration_keeps_keywords_the_ladder_never_surfaced(
    conn: sqlite3.Connection,
) -> None:
    """These are the majority of a real sample, and the ones the fix is for.

    The query used to require a non-NULL prefix_depth, which silently dropped
    every keyword autocomplete cannot see — training the components that exist
    to score them on only the keywords that never needed them.
    """
    mock_charts()
    respx.get(ITUNES_URL).mock(return_value=httpx.Response(200, text=SERP_BODY))
    respx.get(HINTS_URL).mock(return_value=httpx.Response(200, text=EMPTY_HINTS))
    row = track_keyword(conn, "zzqxwvj")
    await run(conn, [row])

    repo.write_demand_observations(
        conn,
        [repo.DemandWrite(source="appfigures", scale="ordinal_100",
                          keyword="zzqxwvj", country="us", value=40.0)],
    )

    samples = pipeline.calibration_from_db(conn, "appfigures")
    assert len(samples) == 1
    assert samples[0].prefix_depth is None, "measured absence, kept as a sample"
    assert samples[0].rating_mass > 0, "the SERP still measured something"


@respx.mock
async def test_calibration_still_drops_a_failed_fetch(
    conn: sqlite3.Connection,
) -> None:
    """"Apple said no match" and "we never reached Apple" must not be conflated."""
    mock_both(serp=500, hints=500)
    row = track_keyword(conn, "forex")
    await run(conn, [row])

    repo.write_demand_observations(
        conn,
        [repo.DemandWrite(source="appfigures", scale="ordinal_100",
                          keyword="forex", country="us", value=40.0)],
    )

    assert pipeline.calibration_from_db(conn, "appfigures") == []
