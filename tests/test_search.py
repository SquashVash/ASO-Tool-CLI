from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from aso.config import (
    SEARCH_DEPTH_DECAY,
    SEARCH_DEPTH_WEIGHT,
    SEARCH_EXTENSIONS_WEIGHT,
    SEARCH_MAX_PREFIX_QUERIES,
    SEARCH_MIN_PREFIX_LEN,
    SEARCH_NO_MATCH_SCORE,
    SEARCH_RANK_DECAY,
    SEARCH_RANK_WEIGHT,
    SEARCH_RATING_MASS_WEIGHT,
    SEARCH_SAVINGS_CAP,
    SEARCH_SAVINGS_WEIGHT,
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


def test_the_ceiling_needs_depth_one_and_a_long_completion() -> None:
    """100 is reachable but has to be earned on every component at once."""
    assert search.score_from_observations(1, 1, keyword_length=21) == pytest.approx(100.0)


def test_a_short_keyword_at_rank_one_does_not_reach_the_ceiling() -> None:
    """The regression that prompted the reshaping.

    Under the old length-relative depth term this returned exactly 100.0 for
    "75 hard" (7 chars, depth 2, rank 1) — tying instagram, youtube, spotify
    and netflix in a live run. Brevity must not buy the ceiling.
    """
    assert search.score_from_observations(2, 1, keyword_length=7) < 90.0


def test_a_longer_phrase_completed_at_the_same_depth_is_never_penalized() -> None:
    """Same observation, more characters saved: never worth *less*.

    This used to assert strictly greater, because SEARCH_SAVINGS_WEIGHT was
    0.15. Calibration against 66 keywords of measured demand drove that weight
    to 0.0 — characters-saved scored a rank correlation of -0.030, which is
    noise with a wrong sign — so the two now tie. The weak inequality is the
    part that holds under either weighting; the strict one is asserted only
    while the component is actually weighted.
    """
    short = search.score_from_observations(2, 1, keyword_length=7)
    long = search.score_from_observations(2, 1, keyword_length=21)
    assert long >= short
    if SEARCH_SAVINGS_WEIGHT > 0:
        assert long > short


def test_depth_is_absolute_not_relative_to_keyword_length() -> None:
    """A given depth costs the same however long the keyword is.

    The old ratio form meant depth 4 was near-ceiling for a 21-char phrase and
    mid-table for a 7-char one. Holding savings constant, the depth penalty
    between two depths must now be identical at any length.
    """
    drop_short = search.score_from_observations(2, 1, 12) - search.score_from_observations(
        4, 1, 14
    )
    drop_long = search.score_from_observations(2, 1, 30) - search.score_from_observations(
        4, 1, 32
    )
    assert drop_short == pytest.approx(drop_long)


def ladder_only(depth_part: float, savings_part: float, rank_part: float) -> float:
    """What the scorer returns when only the ladder components are supplied.

    Missing components are dropped and the survivors renormalized, so a
    ladder-only call divides by the ladder weights rather than by 1.0. Tests
    that assumed a plain weighted sum silently passed until rating mass
    arrived and made the two differ.
    """
    total = SEARCH_DEPTH_WEIGHT + SEARCH_SAVINGS_WEIGHT + SEARCH_RANK_WEIGHT
    return (
        SEARCH_DEPTH_WEIGHT * depth_part
        + SEARCH_SAVINGS_WEIGHT * savings_part
        + SEARCH_RANK_WEIGHT * rank_part
    ) / total


def test_matching_only_at_full_length_scores_on_rank_and_little_else() -> None:
    # Nothing was saved, and the depth term has decayed. Derived from the
    # constants rather than pinned to literals: these get refitted by
    # `aso calibrate`, and a test that hardcodes them fails on every
    # recalibration for no reason worth reading.
    value = search.score_from_observations(11, 1, keyword_length=11)
    assert value == pytest.approx(
        ladder_only(100.0 * SEARCH_DEPTH_DECAY**10, 0.0, 100.0)
    )


def test_score_falls_as_the_matching_prefix_gets_longer() -> None:
    scores = [search.score_from_observations(d, 1, 11) for d in (2, 4, 6, 8, 11)]
    assert scores == sorted(scores, reverse=True)


def test_score_falls_as_rank_gets_worse() -> None:
    """Weakly, because the fit drove SEARCH_RANK_WEIGHT to 0.0.

    Rank correlates 0.108 with real demand among the keywords autocomplete
    surfaces, and drops out of every blend that includes rating mass. While
    its weight is zero every rank ties and this holds vacuously — asserted as
    non-increasing so it stays true either way, with the strict version guarded
    on the weight so reviving rank re-arms the real check.
    """
    scores = [search.score_from_observations(2, r, 11) for r in (1, 2, 3, 5, 10)]
    assert scores == sorted(scores, reverse=True)
    if SEARCH_RANK_WEIGHT > 0:
        assert scores[0] > scores[-1]


def test_rank_decays_geometrically() -> None:
    """Isolate the rank term by holding depth and savings fixed."""
    base = search.score_from_observations(11, 1, 11)
    rank_points = SEARCH_RANK_WEIGHT * 100.0
    assert search.score_from_observations(11, 2, 11) == pytest.approx(
        base - rank_points * (1 - SEARCH_RANK_DECAY)
    )
    assert search.score_from_observations(11, 3, 11) == pytest.approx(
        base - rank_points * (1 - SEARCH_RANK_DECAY**2)
    )


def test_ranks_past_the_list_length_are_floored_not_extrapolated() -> None:
    assert search.score_from_observations(2, 10, 11) == search.score_from_observations(
        2, 99, 11
    )


def test_no_match_scores_the_floor_not_zero() -> None:
    """A true zero would collapse opportunity, which multiplies by this."""
    assert search.score_from_observations(None, None, 11) == SEARCH_NO_MATCH_SCORE
    assert search.score_from_observations(4, None, 11) == SEARCH_NO_MATCH_SCORE
    assert SEARCH_NO_MATCH_SCORE > 0


def test_a_keyword_matching_only_as_itself_saves_nothing() -> None:
    """No savings credit when the completion did no completing."""
    assert search.score_from_observations(2, 1, keyword_length=2) == pytest.approx(
        ladder_only(100.0 * SEARCH_DEPTH_DECAY, 0.0, 100.0)
    )
    assert search.score_from_observations(1, 1, keyword_length=1) == pytest.approx(
        ladder_only(100.0, 0.0, 100.0)
    )


def test_savings_credit_is_capped_not_unbounded() -> None:
    """Past the cap, extra keyword length stops adding score.

    Note this passes vacuously while SEARCH_SAVINGS_WEIGHT is 0.0 — every
    length ties because the component contributes nothing. The cap itself is
    asserted directly below so the guarantee survives that weighting.
    """
    at_cap = search.score_from_observations(2, 1, keyword_length=22)
    far_past = search.score_from_observations(2, 1, keyword_length=40)
    assert at_cap == pytest.approx(far_past)


def test_the_savings_component_itself_saturates_at_the_cap() -> None:
    """The cap, tested on the component rather than through the weighted score.

    `score_from_observations` currently hides this: the fitted savings weight
    is 0.0, so the composite ties at every length whether or not the cap works.
    A guarantee only observable through a zero-weighted term is not tested.
    """
    from aso.config import SEARCH_SAVINGS_CAP

    def savings_component(keyword_length: int, depth: int = 2) -> float:
        saved = max(0, keyword_length - depth)
        return 100.0 * min(saved, SEARCH_SAVINGS_CAP) / SEARCH_SAVINGS_CAP

    assert savings_component(2 + SEARCH_SAVINGS_CAP) == pytest.approx(100.0)
    assert savings_component(2 + SEARCH_SAVINGS_CAP * 3) == pytest.approx(100.0)
    assert savings_component(2) == pytest.approx(0.0)


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


def sample(depth, rank, length, impressions, *, extensions=0, rating_mass=0.0):
    return search.CalibrationSample(
        depth, rank, length, impressions,
        extensions=extensions, rating_mass=rating_mass,
    )


def all_weights(fit):
    """Every weight the fit produced, in `COMPONENT_NAMES` order.

    Assertions must cover all five. Checking only the three original ones
    passed for as long as they summed to 1 and then broke the moment the
    simplex had somewhere else to put mass — which is exactly the bug it
    should have caught, not fallen over on.
    """
    return (
        fit.depth_weight, fit.savings_weight, fit.rank_weight,
        fit.extensions_weight, fit.rating_mass_weight,
    )


def synthetic_samples(n=60, *, depth_decay=0.75, rank_decay=0.70, cap=15, seed=11):
    """Samples whose impressions really are generated by a known parameter set."""
    import random

    rng = random.Random(seed)
    out = []
    for _ in range(n):
        length = rng.randint(4, 25)
        depth = rng.randint(1, min(8, length))
        rank = rng.randint(1, 10)
        truth = (
            0.60 * 100 * depth_decay ** (depth - 1)
            + 0.15 * 100 * min(length - depth, cap) / cap
            + 0.25 * 100 * rank_decay ** (rank - 1)
        )
        out.append(sample(depth, rank, length, 10 ** (truth / 25.0)))
    return out


def test_calibrate_refuses_to_fit_too_few_points() -> None:
    """Six parameters against a dozen points is decoration, not calibration."""
    with pytest.raises(search.NotEnoughData) as excinfo:
        search.calibrate([sample(4, 1, 11, 1000.0)] * 5)
    assert "at least" in str(excinfo.value)


def test_calibrate_recovers_a_known_parameter_set() -> None:
    fit = search.calibrate(synthetic_samples())
    assert fit.n_samples == 60
    assert fit.spearman > 0.95, "should reproduce an ordering it was generated from"


def test_calibrate_returns_weights_on_the_simplex() -> None:
    """The score must stay inside 0-100, which requires non-negative weights summing to 1."""
    fit = search.calibrate(synthetic_samples())
    weights = all_weights(fit)
    assert len(weights) == len(search.COMPONENT_NAMES)
    assert all(w >= 0.0 for w in weights)
    assert sum(weights) == pytest.approx(1.0)


def test_calibrate_reports_the_current_constants_as_a_baseline() -> None:
    """Without this you cannot tell whether calibrating actually helped."""
    fit = search.calibrate(synthetic_samples())
    assert -1.0 <= fit.baseline_spearman <= 1.0
    assert fit.improved == (fit.spearman > fit.baseline_spearman)


def test_calibrate_names_constants_exactly_as_config_does() -> None:
    """The output is meant to be pasted into config.py."""
    from aso import config

    fit = search.calibrate(synthetic_samples())
    for name in fit.as_config():
        assert hasattr(config, name), f"{name} is not a real config constant"


def test_calibrate_refuses_a_target_that_does_not_vary() -> None:
    """Every keyword identical: there is no ordering to fit, so don't invent one."""
    with pytest.raises(search.DegenerateSample):
        search.calibrate([sample(3, 2, 10, 500.0) for _ in range(30)])


def test_calibrate_refuses_a_sample_dominated_by_a_floor_value() -> None:
    """The first real run: 21 of 26 AppFigures keywords sat at the floor of 5.

    Enough rows to pass the count check, but almost no ordering — a fit here
    would report a confident correlation backed by nothing.
    """
    floor = [sample(4, 3, 12, 5.0) for _ in range(21)]
    spread = [sample(2, 1, 12, v) for v in (40.0, 36.0, 24.0, 9.0, 15.0)]
    with pytest.raises(search.DegenerateSample) as excinfo:
        search.calibrate(floor + spread)
    assert "barely varies" in str(excinfo.value)


def test_a_degenerate_sample_is_still_a_not_enough_data_error() -> None:
    """So callers that already handle the count guard keep working."""
    assert issubclass(search.DegenerateSample, search.NotEnoughData)


def test_calibrate_never_returns_a_fit_worse_than_the_baseline_it_reports() -> None:
    """Selection must optimize the metric the fit is graded on.

    The first real run selected on RMSE and reported Spearman, and returned a
    fit scoring 0.564 against the incumbent constants' 0.579 — worse than what
    it was meant to replace. The current constants are always reachable within
    the grid, so a Spearman-first search can never do worse than tie.
    """
    fit = search.calibrate(synthetic_samples(n=50))
    assert fit.spearman >= fit.baseline_spearman - 1e-9


def test_calibrate_flags_constants_pinned_to_the_grid_edge() -> None:
    """An edge means the optimum is outside the searched range, not at the edge."""
    from aso import config

    # Impossible to satisfy inside the grid: demand that rises with depth.
    samples = [
        sample(depth, 1, 20, float(depth) * 10.0)
        for depth in (1, 2, 3, 4, 5, 6, 7, 8)
        for _ in range(3)
    ]
    fit = search.calibrate(samples)
    if fit.depth_decay in (
        config.CALIBRATION_DEPTH_DECAY_GRID[0],
        config.CALIBRATION_DEPTH_DECAY_GRID[-1],
    ):
        assert "SEARCH_DEPTH_DECAY" in fit.at_grid_edge


def test_a_healthy_fit_reports_no_grid_edges_for_depth() -> None:
    fit = search.calibrate(synthetic_samples(n=60))
    assert isinstance(fit.at_grid_edge, tuple)
    assert fit.distinct_targets > 1


def test_calibrate_ignores_keywords_with_no_measured_impressions() -> None:
    usable = synthetic_samples(n=30)
    padded = usable + [sample(3, 1, 10, 0.0) for _ in range(10)]
    assert search.calibrate(padded).n_samples == 30


def test_calibrated_constants_can_be_fed_back_through_the_mapping() -> None:
    """The fit is only useful if its output is the mapping's input."""
    fit = search.calibrate(synthetic_samples())
    config = fit.as_config()
    assert 0.0 < config["SEARCH_DEPTH_DECAY"] <= 1.0
    assert 0.0 < config["SEARCH_RANK_DECAY"] <= 1.0
    assert config["SEARCH_SAVINGS_CAP"] > 0


# --- spearman --------------------------------------------------------------


def test_spearman_is_one_for_a_perfect_ordering() -> None:
    assert search.spearman([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]) == pytest.approx(1.0)


def test_spearman_is_minus_one_for_a_reversed_ordering() -> None:
    assert search.spearman([1.0, 2.0, 3.0], [30.0, 20.0, 10.0]) == pytest.approx(-1.0)


def test_spearman_handles_ties_without_dividing_by_zero() -> None:
    assert search.spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0


def test_spearman_is_rank_based_not_value_based() -> None:
    """A monotone but wildly non-linear relationship is still a perfect ordering."""
    assert search.spearman([1.0, 2.0, 3.0], [1.0, 10.0, 10_000.0]) == pytest.approx(1.0)


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
    # Stated as a gap and a ratio, not as absolute thresholds. Calibration
    # steepened SEARCH_DEPTH_DECAY from 0.80 to 0.55, which pushed every
    # mid-depth score down in absolute terms without changing any ordering —
    # the old `head > 65` was measuring the decay constant, not the claim.
    # The score is ordinal; only the separation is meaningful.
    assert search.score(head) > search.score(tail) * 3
    assert search.score(head) - search.score(tail) > 25


# --- demand scales ---------------------------------------------------------


def ordinal_sample(depth, rank, length, popularity):
    return search.CalibrationSample(
        depth, rank, length, popularity, scale=search.SCALE_ORDINAL_100
    )


def test_an_ordinal_target_is_not_log_compressed() -> None:
    """log10 of a 0-100 rank would squash the top and distort the ordering.

    Three evenly spaced popularity scores must stay evenly spaced as targets.
    """
    samples = [ordinal_sample(2, 1, 10, p) for p in (20.0, 50.0, 80.0)]
    targets = search._targets(samples)
    assert targets == pytest.approx([0.0, 50.0, 100.0])


def test_a_count_target_is_log_compressed() -> None:
    """Impressions span orders of magnitude; without log, head terms own the fit."""
    samples = [
        search.CalibrationSample(2, 1, 10, v, scale=search.SCALE_COUNT)
        for v in (10.0, 1_000.0, 100_000.0)
    ]
    targets = search._targets(samples)
    assert targets[1] == pytest.approx(50.0, abs=1.0), "the middle sits mid-scale"
    assert targets[1] > 10.0, "a linear target would have put it near zero"


def test_mixing_scales_in_one_fit_is_refused() -> None:
    """An impression count and a popularity rank are not commensurable."""
    mixed = [
        search.CalibrationSample(2, 1, 10, 5000.0, scale=search.SCALE_COUNT),
        ordinal_sample(3, 2, 10, 50.0),
    ]
    with pytest.raises(ValueError) as excinfo:
        search._targets(mixed)
    assert "scale" in str(excinfo.value)


def test_calibrating_against_ordinal_popularity_works_end_to_end() -> None:
    """The AppFigures path: fit against a 0-100 rank rather than impressions."""
    import random

    rng = random.Random(17)
    samples = []
    for _ in range(40):
        length = rng.randint(4, 25)
        depth = rng.randint(1, min(8, length))
        rank = rng.randint(1, 10)
        popularity = (
            0.55 * 100 * 0.78 ** (depth - 1)
            + 0.15 * 100 * min(length - depth, 15) / 15
            + 0.30 * 100 * 0.72 ** (rank - 1)
        )
        samples.append(ordinal_sample(depth, rank, length, popularity))

    fit = search.calibrate(samples)
    assert fit.n_samples == 40
    assert fit.spearman > 0.95
    assert sum(all_weights(fit)) == pytest.approx(1.0)


def test_a_degenerate_ordinal_sample_does_not_divide_by_zero() -> None:
    identical = [ordinal_sample(3, 2, 10, 50.0) for _ in range(25)]
    assert search._targets(identical) == [50.0] * 25


# --- cross-validation ------------------------------------------------------


def test_cross_validation_reports_an_out_of_sample_number() -> None:
    fit = search.calibrate(synthetic_samples(n=60))
    assert fit.cv_spearman is not None
    assert -1.0 <= fit.cv_spearman <= 1.0


def test_cross_validated_is_not_above_in_sample_on_real_shaped_noise() -> None:
    """In-sample is the best grid point on its own data; CV is honest.

    On the first real run the gap was 0.320 -> 0.185.
    """
    import random

    rng = random.Random(4)
    noisy = [
        ordinal_sample(
            rng.randint(1, 8), rng.randint(1, 10), rng.randint(6, 25),
            float(rng.randint(5, 70)),
        )
        for _ in range(60)
    ]
    fit = search.calibrate(noisy)
    assert fit.cv_spearman is not None
    assert fit.cv_spearman <= fit.spearman + 1e-9


def test_improved_is_judged_on_the_cross_validated_number() -> None:
    """Comparing in-sample against a no-fitting baseline is rigged."""
    fit = search.calibrate(synthetic_samples(n=60))
    assert fit.improved == (fit.cv_spearman > fit.baseline_spearman)
    assert fit.margin == pytest.approx(fit.cv_spearman - fit.baseline_spearman)


def test_improved_falls_back_to_in_sample_without_cross_validation() -> None:
    fit = search.Calibration(
        depth_decay=0.8, rank_decay=0.8, savings_cap=20, extensions_cap=6,
        depth_weight=0.5, savings_weight=0.15, rank_weight=0.35,
        extensions_weight=0.0, rating_mass_weight=0.0,
        n_samples=25, rmse=1.0, spearman=0.6, baseline_spearman=0.4,
        cv_spearman=None,
    )
    assert fit.improved is True
    assert fit.margin == pytest.approx(0.2)


def test_a_sample_too_small_to_fold_reports_no_cross_validation() -> None:
    """Below 2x the fold count there is nothing honest to hold out."""
    tiny = [
        ordinal_sample(d, r, 12, float(v))
        for d, r, v in zip(
            (1, 2, 3, 4, 5, 6, 7, 8, 1),
            (1, 2, 3, 4, 5, 6, 7, 8, 9),
            (10, 20, 30, 40, 50, 60, 70, 80, 90),
        )
    ]
    fit = search.calibrate(tiny, min_samples=9)
    assert fit.cv_spearman is None
    assert fit.improved == (fit.spearman > fit.baseline_spearman)


# --- extensions: the signal for keywords the ladder cannot see -------------


def test_extensions_are_counted_from_the_full_length_prefix() -> None:
    """A stem Apple completes ten ways is a demanded stem, not an absent one."""
    lists = {
        "insta": ["instagram", "insta story", "instacart"],
        "inst": ["instagram", "instacart"],
    }

    async def probe(prefix: str):
        return lists.get(prefix, [])

    observation = asyncio.run(search.observe("insta", probe))
    # Apple offers completions, never the query echoed back as its own
    # completion, so no rung of the ladder ever surfaces "insta" itself...
    assert observation.prefix_depth is None
    assert observation.hint_rank is None
    # ...yet all three suggestions extend it. This keyword measures 75/100 at
    # AppFigures and scored the dead-term floor before this component existed.
    assert observation.extensions == 3


def test_a_suggestion_equal_to_the_keyword_is_not_an_extension() -> None:
    """Equal is not extending — it is a rank-1 ladder hit, counted elsewhere."""

    async def probe(prefix: str):
        return ["habit", "habit tracker"]

    observation = asyncio.run(search.observe("habit", probe))
    assert observation.prefix_depth is not None, "an exact match is a ladder hit"
    assert observation.extensions == 1


def test_extensions_ignore_suggestions_that_merely_contain_the_keyword() -> None:
    """Prefix match, not substring: `the habit burger grill` is not `habit` demand."""

    async def probe(prefix: str):
        return ["habit tracker", "the habit burger grill", "habitica"]

    observation = asyncio.run(search.observe("habit", probe))
    assert observation.extensions == 2


def test_a_keyword_nothing_extends_records_zero_not_none() -> None:
    """Measured absence and no measurement are different inputs to the scorer."""

    async def probe(prefix: str):
        return ["something else entirely"]

    assert asyncio.run(search.observe("zzqx", probe)).extensions == 0


def test_extensions_raise_the_score_of_an_unsurfaced_keyword() -> None:
    """The whole point: split the tie that every no-match keyword used to share."""
    dead = search.score_from_observations(None, None, 5, extensions=0, rating_mass=0.0)
    stem = search.score_from_observations(None, None, 5, extensions=10, rating_mass=0.0)
    assert stem > dead
    assert dead == pytest.approx(SEARCH_NO_MATCH_SCORE)


# --- renormalization when an instrument fails ------------------------------


def test_a_missing_component_is_dropped_not_scored_as_zero() -> None:
    """An unmeasured instrument must not read as a measured zero.

    Asserted on `extensions` rather than `rating_mass`. Rating mass moved to
    the competition side (SEARCH_RATING_MASS_WEIGHT is 0.00), and a
    zero-weighted component is dropped by the `weight > 0` filter whether it
    is None or 0.0 — so testing renormalization through it would compare two
    identical numbers and pass no matter what the renormalization did.
    """
    without = search.score_from_observations(2, 1, 10, extensions=None)
    as_zero = search.score_from_observations(2, 1, 10, extensions=0)
    assert without > as_zero


def test_rating_mass_no_longer_moves_the_demand_score() -> None:
    """It is scored as competition now — counting it here too would double it."""
    assert search.score_from_observations(
        2, 1, 10, extensions=4, rating_mass=100.0
    ) == pytest.approx(
        search.score_from_observations(2, 1, 10, extensions=4, rating_mass=0.0)
    )


def test_dropping_a_component_keeps_the_score_in_range() -> None:
    for kwargs in (
        {},
        {"extensions": 10},
        {"rating_mass": 100.0},
        {"extensions": 10, "rating_mass": 100.0},
    ):
        value = search.score_from_observations(1, 1, 10, **kwargs)
        assert SEARCH_NO_MATCH_SCORE <= value <= 100.0


def test_every_component_present_and_maximal_scores_one_hundred() -> None:
    """Proves the weights renormalize to exactly 1, not to something near it."""
    assert search.score_from_observations(
        1, 1, 1 + SEARCH_SAVINGS_CAP, extensions=99, rating_mass=100.0
    ) == pytest.approx(100.0)


def test_nothing_measurable_at_all_is_the_floor_not_zero() -> None:
    assert search.score_from_observations(None, None, 8) > 0.0
