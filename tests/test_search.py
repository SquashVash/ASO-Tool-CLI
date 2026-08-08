from __future__ import annotations

from collections.abc import Sequence

import pytest

from aso.config import (
    SEARCH_MAX_PREFIX_QUERIES,
    SEARCH_MIN_PREFIX_LEN,
    SEARCH_NO_MATCH_SCORE,
)
from aso.scoring import search
from aso.scoring.opportunity import opportunity


def fake_probe(responses: dict[str, list[str]], *, log: list[str] | None = None):
    """A probe backed by a dict. Unlisted prefixes return no suggestions."""

    async def probe(prefix: str) -> Sequence[str]:
        if log is not None:
            log.append(prefix)
        return responses.get(prefix, [])

    return probe


def ladder_where_keyword_appears_from(keyword: str, min_prefix: int, rank: int = 1):
    """Suggestions where `keyword` surfaces for every prefix >= min_prefix."""
    responses: dict[str, list[str]] = {}
    for length in range(1, len(keyword) + 1):
        prefix = keyword[:length]
        if length >= min_prefix:
            padding = [f"filler {i}" for i in range(rank - 1)]
            responses[prefix] = padding + [keyword]
        else:
            responses[prefix] = ["something else"]
    return responses


# --- prefix ladder construction --------------------------------------------


def test_ladder_runs_from_full_length_down_to_the_floor() -> None:
    assert search.prefix_lengths("forex", min_len=2, max_queries=12) == [5, 4, 3, 2]


def test_ladder_is_sampled_when_it_exceeds_the_query_cap() -> None:
    lengths = search.prefix_lengths("japanese candlestick patterns", max_queries=12)
    assert len(lengths) <= 12
    assert lengths[0] == 29, "the full keyword is always tried"
    assert lengths[-1] == SEARCH_MIN_PREFIX_LEN, "the shortest rung is always tried"
    assert lengths == sorted(lengths, reverse=True)


def test_ladder_respects_the_configured_cap_by_default() -> None:
    lengths = search.prefix_lengths("a" * 60)
    assert len(lengths) <= SEARCH_MAX_PREFIX_QUERIES


def test_short_keyword_is_still_queried_at_its_own_length() -> None:
    assert search.prefix_lengths("x", min_len=2, max_queries=12) == [1]
    assert search.prefix_lengths("ab", min_len=2, max_queries=12) == [2]


def test_empty_keyword_has_no_ladder() -> None:
    assert search.prefix_lengths("") == []


# --- the walk --------------------------------------------------------------


async def test_finds_the_shortest_matching_prefix_and_its_rank() -> None:
    responses = ladder_where_keyword_appears_from("candlestick", 4, rank=2)
    obs = await search.observe("candlestick", fake_probe(responses))
    assert obs.prefix_depth == 4
    assert obs.hint_rank == 2
    assert obs.keyword_length == 11


async def test_walk_short_circuits_on_the_first_miss() -> None:
    """Once a prefix misses, nothing shorter is queried."""
    log: list[str] = []
    responses = ladder_where_keyword_appears_from("candlestick", 8)
    await search.observe("candlestick", fake_probe(responses, log=log))
    # Tried 11, 10, 9, 8, then 7 missed and the walk stopped.
    assert log == ["candlestick", "candlestic", "candlesti", "candlest", "candles"]
    assert "candle" not in log


async def test_keyword_that_never_appears_has_no_observations() -> None:
    obs = await search.observe("candlestick", fake_probe({}))
    assert obs.prefix_depth is None
    assert obs.hint_rank is None
    assert obs.queries_used == 1, "one miss at full length ends the walk"


async def test_keyword_appearing_only_at_full_length() -> None:
    obs = await search.observe(
        "candlestick", fake_probe({"candlestick": ["candlestick"]})
    )
    assert obs.prefix_depth == 11
    assert obs.hint_rank == 1


async def test_walk_normalizes_the_keyword_before_probing() -> None:
    log: list[str] = []
    await search.observe("  Candle  Stick ", fake_probe({}, log=log))
    assert log == ["candle stick"]


async def test_single_character_keyword_is_walked_not_skipped() -> None:
    obs = await search.observe("x", fake_probe({"x": ["x"]}))
    assert obs.prefix_depth == 1
    assert obs.hint_rank == 1
    assert obs.queries_used == 1


async def test_empty_keyword_makes_no_requests() -> None:
    log: list[str] = []
    obs = await search.observe("   ", fake_probe({}, log=log))
    assert log == []
    assert obs.prefix_depth is None


async def test_probe_failures_propagate_rather_than_reading_as_no_volume() -> None:
    async def failing(prefix: str):
        raise RuntimeError("403 from Apple")

    with pytest.raises(RuntimeError):
        await search.observe("candlestick", failing)


async def test_sampling_is_reported_so_coarse_depth_is_visible() -> None:
    obs = await search.observe(
        "japanese candlestick patterns",
        fake_probe(ladder_where_keyword_appears_from("japanese candlestick patterns", 2)),
    )
    assert obs.sampled is True
    assert obs.queries_used <= SEARCH_MAX_PREFIX_QUERIES


# --- the mapping -----------------------------------------------------------


def test_rank_one_at_the_shortest_prefix_is_the_maximum() -> None:
    assert search.score_from_observations(2, 1, keyword_length=11) == pytest.approx(100.0)


def test_matching_only_at_full_length_scores_on_rank_alone() -> None:
    # depth contributes 0; rank 1 contributes its full weight.
    assert search.score_from_observations(11, 1, keyword_length=11) == pytest.approx(30.0)


def test_score_falls_as_the_matching_prefix_gets_longer() -> None:
    scores = [search.score_from_observations(d, 1, 11) for d in (2, 4, 6, 8, 11)]
    assert scores == sorted(scores, reverse=True)


def test_score_falls_as_rank_gets_worse() -> None:
    scores = [search.score_from_observations(2, r, 11) for r in (1, 2, 3, 5, 10)]
    assert scores == sorted(scores, reverse=True)


def test_rank_decays_geometrically() -> None:
    assert search.score_from_observations(11, 2, 11) == pytest.approx(30.0 * 0.82)
    assert search.score_from_observations(11, 3, 11) == pytest.approx(30.0 * 0.82**2)


def test_ranks_past_the_list_length_are_floored_not_extrapolated() -> None:
    assert search.score_from_observations(2, 10, 11) == search.score_from_observations(
        2, 99, 11
    )


def test_no_match_scores_the_floor_not_zero() -> None:
    """A true zero would collapse opportunity, which multiplies by this."""
    assert search.score_from_observations(None, None, 11) == SEARCH_NO_MATCH_SCORE
    assert search.score_from_observations(4, None, 11) == SEARCH_NO_MATCH_SCORE
    assert SEARCH_NO_MATCH_SCORE > 0


def test_short_keywords_score_on_rank_at_full_strength_depth() -> None:
    # A 2-char keyword's only prefix is itself; surfacing at all is maximal.
    assert search.score_from_observations(2, 1, keyword_length=2) == pytest.approx(100.0)
    assert search.score_from_observations(1, 1, keyword_length=1) == pytest.approx(100.0)


def test_scores_stay_in_bounds_across_the_whole_space() -> None:
    for length in range(1, 40):
        for depth in range(1, length + 1):
            for rank in (1, 5, 10):
                value = search.score_from_observations(depth, rank, length)
                assert 0.0 <= value <= 100.0


def test_score_of_an_observation_matches_the_pure_mapping() -> None:
    obs = search.LadderObservation(prefix_depth=3, hint_rank=2, keyword_length=11)
    assert search.score(obs) == search.score_from_observations(3, 2, 11)


def test_stored_observations_rescore_without_refetching() -> None:
    """The reproducibility promise for the search side.

    snapshots stores prefix_depth and hint_rank; keyword length comes from the
    keywords table. That is everything the mapping needs.
    """
    stored_depth, stored_rank, length = 4, 2, 11
    original = search.score_from_observations(stored_depth, stored_rank, length)
    recalibrated = search.score_from_observations(stored_depth, stored_rank, length)
    assert original == recalibrated


# --- calibration hook ------------------------------------------------------


def test_calibrate_is_explicitly_unimplemented() -> None:
    with pytest.raises(NotImplementedError) as excinfo:
        search.calibrate([(4, 1, 11, 1000.0)])
    assert "Search Ads" in str(excinfo.value)


# --- opportunity -----------------------------------------------------------


def test_opportunity_matches_the_documented_formula() -> None:
    assert opportunity(80.0, 25.0) == pytest.approx(60.0)


def test_uncontested_volume_passes_through_intact() -> None:
    assert opportunity(80.0, 0.0) == pytest.approx(80.0)


def test_a_fully_contested_keyword_is_worth_nothing() -> None:
    assert opportunity(100.0, 100.0) == pytest.approx(0.0)


def test_moderate_uncontested_volume_beats_high_contested_volume() -> None:
    assert opportunity(40.0, 10.0) > opportunity(95.0, 80.0)


def test_unknown_inputs_give_an_unknown_result() -> None:
    """Not zero, and certainly not a default that tops the ranking."""
    assert opportunity(None, 20.0) is None
    assert opportunity(80.0, None) is None
    assert opportunity(None, None) is None


def test_opportunity_clamps_out_of_range_inputs() -> None:
    assert opportunity(120.0, -10.0) == pytest.approx(100.0)


# --- end to end through the ladder -----------------------------------------


async def test_high_volume_keyword_scores_far_above_a_long_tail_one() -> None:
    head = await search.observe(
        "candlestick", fake_probe(ladder_where_keyword_appears_from("candlestick", 3, 1))
    )
    tail = await search.observe(
        "candlestick pattern cheat sheet",
        fake_probe({"candlestick pattern cheat sheet": ["candlestick pattern cheat sheet"]}),
    )
    assert search.score(head) > 80
    assert search.score(tail) < 35
