from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from aso import config
from aso.clients import itunes
from aso.clients.charts import ChartIndex
from aso.clients.itunes import AppRecord, Serp
from aso.scoring import competition as comp

from .conftest import FIXTURES

NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)
DIVISOR = config.COMP_RATING_COUNT_LOG_DIVISOR


def app(
    track_id: int = 1,
    name: str | None = "Some App",
    *,
    subtitle: str | None = None,
    seller: str | None = "Acme",
    ratings: int | None = 1000,
    stars: float | None = 4.5,
    days_old: float | None = 30,
) -> AppRecord:
    released = None
    if days_old is not None:
        released = (NOW - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")
    return AppRecord(
        track_id=track_id,
        track_name=name,
        subtitle=subtitle,
        seller_name=seller,
        user_rating_count=ratings,
        average_user_rating=stars,
        current_version_release_date=released,
    )


def serp(apps: list[AppRecord], keyword: str = "day trading", count: int | None = None) -> Serp:
    return Serp(
        keyword=keyword,
        country="us",
        apps=apps,
        result_count=len(apps) if count is None else count,
        captured_at="2026-08-08T00:00:00Z",
        from_cache=False,
    )


# --- comp_rating_count -----------------------------------------------------


def test_rating_count_matches_the_documented_formula() -> None:
    apps = [app(i, ratings=1000) for i in range(10)]
    expected = min(math.log10(1001) / DIVISOR, 1) * 100
    assert comp.rating_count_component(apps) == pytest.approx(expected)


def test_rating_count_saturates_at_a_hundred_thousand() -> None:
    """The old ceiling was a million, a level essentially no top 10 reaches.

    Anything at or above the saturation point is equally unwinnable, so the
    scale is better spent below it.
    """
    assert comp.rating_count_component([app(ratings=100_000)]) == pytest.approx(100.0)
    assert comp.rating_count_component([app(ratings=10_000_000)]) == pytest.approx(100.0)
    assert comp.rating_count_component([app(ratings=10_000)]) < 100.0


def test_rating_count_uses_the_median_not_the_mean() -> None:
    """One 5M-rating giant among nine tiny apps must not dominate."""
    apps = [app(i, ratings=10) for i in range(9)] + [app(99, ratings=5_000_000)]
    assert comp.rating_count_component(apps) == pytest.approx(
        min(math.log10(11) / DIVISOR, 1) * 100
    )


def test_zero_ratings_count_as_a_real_observation() -> None:
    # An app nobody has rated genuinely has zero review mass.
    assert comp.rating_count_component([app(ratings=0)]) == pytest.approx(0.0)


def test_missing_rating_counts_are_excluded_not_zeroed() -> None:
    mixed = [app(1, ratings=None), app(2, ratings=1000), app(3, ratings=1000)]
    assert comp.rating_count_component(mixed) == comp.rating_count_component(
        [app(2, ratings=1000)]
    )


def test_rating_count_is_none_when_nothing_reports_one() -> None:
    assert comp.rating_count_component([app(1, ratings=None)]) is None
    assert comp.rating_count_component([]) is None


# --- comp_exact_match ------------------------------------------------------


def test_exact_match_counts_the_fraction_of_the_top_ten() -> None:
    apps = [app(i, name="Day Trading Pro") for i in range(4)]
    apps += [app(i + 10, name="Unrelated") for i in range(6)]
    assert comp.exact_match_component(apps, "day trading") == pytest.approx(40.0)


def test_exact_match_ignores_case_and_extra_whitespace() -> None:
    apps = [app(name="DAY   TRADING simulator")]
    assert comp.exact_match_component(apps, "  Day Trading ") == pytest.approx(100.0)


def test_scattered_tokens_score_below_a_contiguous_phrase() -> None:
    """The old version scored this 0. Ten such apps still rank for the term."""
    scattered = comp.exact_match_component([app(name="Trading Day Planner")], "day trading")
    contiguous = comp.exact_match_component([app(name="Day Trading Pro")], "day trading")
    assert scattered == pytest.approx(75.0)
    assert contiguous == pytest.approx(100.0)
    assert scattered is not None and contiguous is not None and scattered < contiguous


def test_partial_token_coverage_scores_proportionally() -> None:
    apps = [app(name="Journal Daily")]
    # 1 of 2 tokens, times the 0.75 ceiling for a non-contiguous match.
    assert comp.exact_match_component(apps, "smart journal") == pytest.approx(37.5)


def test_a_totally_invisible_match_is_unknown_not_zero() -> None:
    """Nothing visible does not mean nothing there.

    The Search API hides the subtitle and the keyword field. If all ten apps
    rank for a term and none shows a trace of it, Apple matched them on
    something we cannot read -- so the honest answer is None, and `combine`
    renormalizes. Scoring 0 would assert an absence never observed.
    """
    apps = [app(i, name="Weather Radar") for i in range(10)]
    assert comp.exact_match_component(apps, "day trading") is None


def test_one_visible_match_is_enough_to_make_the_component_real() -> None:
    """Partial visibility is evidence. Only a total blank is unknown."""
    apps = [app(1, name="Weather Radar"), app(2, name="Trading Post")]
    value = comp.exact_match_component(apps, "day trading")
    assert value is not None and value > 0.0


def test_single_word_keywords_keep_the_old_binary_behaviour() -> None:
    """For a one-token keyword the phrase and the token coincide."""
    assert comp.exact_match_component([app(name="Journal Pro")], "journal") == pytest.approx(100.0)
    # ...except that a clean sweep of non-matches is now unknown, not zero.
    assert comp.exact_match_component([app(name="Weather")], "journal") is None


def test_tokens_match_as_substrings_so_stems_count() -> None:
    """Apple's matching is stem-aware; "Journaling" competes for "journal"."""
    apps = [app(name="Journaling Daily")]
    assert comp.exact_match_component(apps, "smart journal") == pytest.approx(37.5)


def test_exact_match_reads_the_subtitle_when_one_exists() -> None:
    """The Search API omits subtitle today, but the matcher is ready for it."""
    apps = [app(name="Ticker", subtitle="Day Trading Made Simple")]
    assert comp.exact_match_component(apps, "day trading") == pytest.approx(100.0)


def test_missing_titles_are_a_non_match_not_a_skip() -> None:
    apps = [app(1, name="Day Trading Pro"), app(2, name=None)]
    assert comp.exact_match_component(apps, "day trading") == pytest.approx(50.0)


def test_exact_match_is_none_for_an_empty_serp() -> None:
    assert comp.exact_match_component([], "day trading") is None


# --- comp_stars ------------------------------------------------------------


def test_stars_are_the_median_mapped_from_the_meaningful_floor() -> None:
    apps = [app(1, stars=4.6), app(2, stars=5.0), app(3, stars=4.2)]
    # median 4.6, mapped from [4.0, 5.0] onto 0-100
    assert comp.stars_component(apps) == pytest.approx(60.0)


def test_the_floor_spreads_out_the_range_that_actually_occurs() -> None:
    """The whole point of the rescale: real SERPs must become distinguishable.

    Divided by 5 these two landed at 84 and 96 — a 12-point gap on a component
    that multiplies review mass. On the anchored ruler they are 20 and 80.
    """
    soft = comp.stars_component([app(stars=4.2)])
    hard = comp.stars_component([app(stars=4.8)])
    assert soft == pytest.approx(20.0)
    assert hard == pytest.approx(80.0)


def test_unrated_apps_are_excluded_from_the_stars_median() -> None:
    """Apple reports averageUserRating 0.0 for unrated apps.

    That is missing data, not a zero-star app. Counting it would halve the
    quality signal of a niche full of unrated entrants.
    """
    apps = [app(1, stars=4.5, ratings=500), app(2, stars=0.0, ratings=0)]
    assert comp.stars_component(apps) == pytest.approx(50.0)


def test_a_genuine_low_rating_clamps_to_zero_rather_than_going_missing() -> None:
    """Below the floor is still a measurement — 0.0, never None.

    The distinction matters: 0.0 scores the keyword as maximally soft on
    quality, while None would make `combine` renormalize it away entirely.
    """
    assert comp.stars_component([app(1, stars=1.0, ratings=25)]) == pytest.approx(0.0)
    assert comp.stars_component([app(1, stars=4.0, ratings=25)]) == pytest.approx(0.0)


def test_stars_are_none_when_no_app_has_a_real_rating() -> None:
    assert comp.stars_component([app(1, stars=0.0, ratings=0)]) is None
    assert comp.stars_component([app(1, stars=None, ratings=None)]) is None
    assert comp.stars_component([]) is None


# --- comp_recency ----------------------------------------------------------


def test_recency_is_100_for_an_app_updated_today() -> None:
    assert comp.recency_component([app(days_old=0)], NOW) == pytest.approx(100.0)


def test_recency_is_zero_at_a_year_and_stays_there() -> None:
    assert comp.recency_component([app(days_old=365)], NOW) == pytest.approx(0.0)
    assert comp.recency_component([app(days_old=2000)], NOW) == pytest.approx(0.0)


def test_recency_is_linear_in_between() -> None:
    assert comp.recency_component([app(days_old=182.5)], NOW) == pytest.approx(50.0)


def test_future_release_dates_clamp_to_fully_fresh() -> None:
    assert comp.recency_component([app(days_old=-10)], NOW) == pytest.approx(100.0)


def test_unparseable_and_missing_dates_are_excluded() -> None:
    apps = [
        AppRecord(track_id=1, current_version_release_date="not a date"),
        AppRecord(track_id=2, current_version_release_date=None),
        app(3, days_old=0),
    ]
    assert comp.recency_component(apps, NOW) == pytest.approx(100.0)


def test_recency_is_none_when_no_date_is_usable() -> None:
    assert comp.recency_component([app(days_old=None)], NOW) is None
    assert comp.recency_component([], NOW) is None


# --- comp_publisher --------------------------------------------------------


SATURATION = config.COMP_PUBLISHER_SATURATION_SLOTS
DECAY = config.COMP_PUBLISHER_RANK_DECAY


def _rank_weighted(strengths: list[float]) -> float:
    """The component's own aggregation, restated for the tests to check against."""
    weights = [1.0 / (1.0 + DECAY * i) for i in range(len(strengths))]
    return sum(w * s * 100 for w, s in zip(weights, strengths)) / sum(weights)


def test_publisher_counts_repeat_sellers_across_the_full_result_set() -> None:
    top = [app(1, seller="Acme"), app(2, seller="Solo")]
    # Acme appears twice overall; the second appearance is outside the top 10.
    full = top + [app(3, seller="Acme")]
    expected = _rank_weighted([math.log(2) / math.log(SATURATION), 0.0])
    assert comp.publisher_component(top, full) == pytest.approx(expected)


def test_a_seller_appearing_once_does_not_count() -> None:
    top = [app(1, seller="Solo"), app(2, seller="Other")]
    assert comp.publisher_component(top, top) == pytest.approx(0.0)


def test_one_publisher_owning_the_space_scores_100() -> None:
    top = [app(i, seller="Acme") for i in range(10)]
    assert comp.publisher_component(top, top) == pytest.approx(100.0)


def test_deeper_catalogues_score_higher_than_a_bare_repeat() -> None:
    """The point of grading: two slots and twenty are no longer the same fact.

    The threshold rule this replaced scored both of these 100.
    """
    top = [app(1, seller="Acme"), app(2, seller="Solo")]
    # Acme holds exactly 2 of the field's slots...
    bare = comp.publisher_component(top, top + [app(10, seller="Acme")])
    # ...versus 6, well past the saturation point.
    deep = comp.publisher_component(top, top + [app(10 + i, seller="Acme") for i in range(5)])
    assert deep is not None and bare is not None
    assert deep > bare
    assert bare > 0.0


def test_publisher_is_rank_weighted_toward_the_top_slots() -> None:
    """Holding ranks 1-2 is a higher wall than holding ranks 9-10."""
    others = [app(50 + i, seller=f"Indie {i}") for i in range(8)]
    high = [app(1, seller="Acme"), app(2, seller="Acme")] + others
    low = others + [app(1, seller="Acme"), app(2, seller="Acme")]
    full_high = high + [app(90 + i, seller="Acme") for i in range(4)]
    full_low = low + [app(90 + i, seller="Acme") for i in range(4)]
    top_heavy = comp.publisher_component(high, full_high)
    bottom_heavy = comp.publisher_component(low, full_low)
    assert top_heavy is not None and bottom_heavy is not None
    assert top_heavy > bottom_heavy


def test_seller_matching_ignores_case_and_spacing() -> None:
    """Two spellings of one publisher must pool into one, not read as two."""
    spelled = [app(1, seller="Acme  Inc"), app(2, seller="ACME INC")]
    canonical = [app(1, seller="Acme Inc"), app(2, seller="Acme Inc")]
    distinct = [app(1, seller="Acme Inc"), app(2, seller="Other Co")]
    assert comp.publisher_component(spelled, spelled) == pytest.approx(
        comp.publisher_component(canonical, canonical)
    )
    assert comp.publisher_component(spelled, spelled) > comp.publisher_component(
        distinct, distinct
    )


def test_unknown_sellers_never_count_as_concentration() -> None:
    top = [app(1, seller=None), app(2, seller=None)]
    assert comp.publisher_component(top, top) == pytest.approx(0.0)


def test_publisher_is_none_for_an_empty_top_set() -> None:
    assert comp.publisher_component([], []) is None


def test_a_fully_diverse_field_is_a_measured_zero_not_unknown() -> None:
    """Ten distinct publishers with no other apps is an observation.

    We looked at the whole field and found no concentration. That is different
    from `comp_exact_match`'s blank, where Apple withholds the fields the
    component would need — and so it scores 0 rather than renormalizing away.
    """
    top = [app(i, seller=f"Indie {i}") for i in range(10)]
    assert comp.publisher_component(top, top) == pytest.approx(0.0)


# --- comp_app_power --------------------------------------------------------


def charts(ranks: dict[int, int], *, loaded: int = 48, depth: int = 100) -> ChartIndex:
    return ChartIndex(country="us", ranks=ranks, charts_loaded=loaded, depth=depth)


def test_app_power_is_none_without_an_index() -> None:
    """No index means the question was never asked."""
    assert comp.app_power_component([app(1)], None) is None


def test_app_power_is_none_when_every_chart_feed_failed() -> None:
    """An index nothing loaded into proves nothing about the storefront.

    Scoring it 0 would say "no app here is popular", which was never observed —
    and would make a storefront Apple stopped serving look uncontested.
    """
    assert comp.app_power_component([app(1)], charts({}, loaded=0)) is None


def test_a_top_10_that_charts_nowhere_is_a_measured_zero() -> None:
    """Unlike a missing index. We pulled every genre's top 100 and looked."""
    apps = [app(i) for i in range(10)]
    assert comp.app_power_component(apps, charts({999: 1})) == pytest.approx(0.0)


def test_the_number_one_app_in_a_category_scores_100() -> None:
    assert comp.app_power_component([app(1)], charts({1: 1})) == pytest.approx(100.0)


def test_charting_at_all_earns_the_floor() -> None:
    """The chart boundary is a cliff, not a slope.

    Rank 100 versus absent is a much bigger fact than rank 100 versus rank 50,
    and a pure log-depth curve gets that backwards.
    """
    deep = comp.app_power_component([app(1)], charts({1: 100}))
    assert deep is not None
    assert deep == pytest.approx(config.COMP_APP_POWER_CHARTED_FLOOR * 100, abs=0.5)
    assert deep > 0.0


def test_app_power_decreases_with_chart_depth() -> None:
    ranks = [comp.app_power_component([app(1)], charts({1: r})) for r in (1, 10, 50, 100)]
    assert all(a > b for a, b in zip(ranks, ranks[1:]))


def test_app_power_is_rank_weighted_toward_the_top_slots() -> None:
    """A giant at rank 1 is a bigger obstacle than the same giant at rank 10."""
    apps = [app(i) for i in range(10)]
    index = charts({0: 1})
    leading = comp.app_power_component(apps, index)
    trailing = comp.app_power_component(list(reversed(apps)), index)
    assert leading is not None and trailing is not None
    assert leading > trailing


def test_more_charted_incumbents_score_higher() -> None:
    apps = [app(i) for i in range(10)]
    one = comp.app_power_component(apps, charts({0: 5}))
    many = comp.app_power_component(apps, charts({i: 5 for i in range(10)}))
    assert one is not None and many is not None
    assert many > one


def test_app_power_is_none_for_an_empty_top_set() -> None:
    assert comp.app_power_component([], charts({1: 1})) is None


def test_a_nonsense_rank_is_treated_as_uncharted() -> None:
    """log(0) is undefined and rank 0 does not exist; neither may raise."""
    assert comp.app_power_component([app(1)], charts({1: 0})) == pytest.approx(0.0)


# --- comp_breadth ----------------------------------------------------------


def test_breadth_matches_the_documented_formula() -> None:
    assert comp.breadth_component(43) == pytest.approx(min(math.log10(44) / 2.3, 1) * 100)


def test_breadth_is_zero_for_no_results() -> None:
    assert comp.breadth_component(0) == pytest.approx(0.0)


def test_breadth_saturates() -> None:
    assert comp.breadth_component(10_000) == pytest.approx(100.0)


# --- comp_incumbent --------------------------------------------------------


def test_incumbent_is_the_geometric_mean_of_its_two_inputs() -> None:
    assert comp.incumbent_component(100.0, 96.0) == pytest.approx(math.sqrt(9600.0))
    assert comp.incumbent_component(64.0, 81.0) == pytest.approx(72.0)


def test_incumbent_needs_both_inputs_to_be_high() -> None:
    """The whole point of the interaction: neither input alone carries it."""
    huge_but_hated = comp.incumbent_component(100.0, 40.0)
    tiny_but_loved = comp.incumbent_component(17.0, 100.0)
    huge_and_loved = comp.incumbent_component(100.0, 96.0)

    assert huge_and_loved > huge_but_hated > tiny_but_loved


def test_incumbent_is_none_when_either_input_is_missing() -> None:
    """Half an interaction is not the claim this component makes."""
    assert comp.incumbent_component(None, 90.0) is None
    assert comp.incumbent_component(90.0, None) is None
    assert comp.incumbent_component(None, None) is None


def test_incumbent_is_zero_when_an_input_is_genuinely_zero() -> None:
    """A measured zero is not a missing value — it must still score."""
    assert comp.incumbent_component(0.0, 100.0) == pytest.approx(0.0)


def test_incumbent_stays_within_bounds() -> None:
    assert comp.incumbent_component(100.0, 100.0) == pytest.approx(100.0)


# --- derive ----------------------------------------------------------------


def test_derive_fills_incumbent_from_the_two_stored_columns() -> None:
    stored = {"comp_rating_count": 64.0, "comp_stars": 81.0}
    assert comp.derive(stored)["comp_incumbent"] == pytest.approx(72.0)


def test_derive_recomputes_rather_than_trusting_a_stored_value() -> None:
    """Formula changes must propagate to old rows on rescore, not be inherited."""
    stale = {"comp_rating_count": 64.0, "comp_stars": 81.0, "comp_incumbent": 999.0}
    assert comp.derive(stale)["comp_incumbent"] == pytest.approx(72.0)


def test_derive_does_not_mutate_its_input() -> None:
    stored = {"comp_rating_count": 64.0, "comp_stars": 81.0}
    comp.derive(stored)
    assert "comp_incumbent" not in stored


# --- combine ---------------------------------------------------------------


def test_combine_is_a_plain_weighted_mean_when_all_present() -> None:
    components = {name: 50.0 for name in comp.COMPETITION_WEIGHTS}
    assert comp.combine(components) == pytest.approx(50.0)


def test_combine_renormalizes_around_missing_components() -> None:
    """A missing component must not drag the score toward zero."""
    components = {name: 80.0 for name in comp.COMPETITION_WEIGHTS}
    components["comp_rating_count"] = None
    assert comp.combine(components) == pytest.approx(80.0)


# --- comp_publisher --------------------------------------------------------


def test_publisher_concentration_raises_the_score() -> None:
    """Calibration made this a weighted term rather than a multiplier."""
    base = {name: 60.0 for name in comp.COMPETITION_WEIGHTS}
    base["comp_publisher"] = 0.0
    concentrated = dict(base, comp_publisher=100.0)
    low, high = comp.combine(base), comp.combine(concentrated)
    assert low is not None and high is not None and high > low


def test_unknown_concentration_leaves_the_score_alone() -> None:
    """Absent data must not move the score in either direction."""
    components = {name: 60.0 for name in comp.COMPETITION_WEIGHTS}
    components["comp_publisher"] = None
    assert comp.combine(components) == pytest.approx(60.0)


def test_the_score_cannot_leave_the_zero_to_one_hundred_range() -> None:
    components = {name: 100.0 for name in comp.COMPETITION_WEIGHTS}
    assert comp.combine(components) == pytest.approx(100.0)


def test_combine_returns_none_when_nothing_is_computable() -> None:
    assert comp.combine({name: None for name in comp.COMPETITION_WEIGHTS}) is None


def test_combine_respects_custom_weights() -> None:
    components = {name: 0.0 for name in comp.COMPETITION_WEIGHTS}
    components["comp_exact_match"] = 100.0
    only_exact = {"comp_exact_match": 1.0}
    assert comp.combine(components, only_exact) == pytest.approx(100.0)


def test_reweighting_stored_components_needs_no_refetch() -> None:
    """The core promise: any stored score can be recomputed from its own row."""
    result = comp.score(serp([app(i, name="Day Trading") for i in range(10)]), now=NOW)
    stored = result.components.as_dict()

    doubled = dict(comp.COMPETITION_WEIGHTS)
    doubled["comp_exact_match"] = 0.5
    rescored = comp.combine(stored, doubled)

    assert rescored is not None
    assert comp.combine(stored) == pytest.approx(result.score)
    assert rescored != pytest.approx(result.score)


# --- score() and edge cases ------------------------------------------------


def test_score_of_a_crowded_keyword_is_high() -> None:
    apps = [
        app(i, name="Day Trading Pro", seller="MegaCorp", ratings=500_000, stars=4.8, days_old=5)
        for i in range(10)
    ]
    result = comp.score(serp(apps, count=50), now=NOW)
    assert result.score is not None and result.score > 70


def test_score_of_a_sleepy_keyword_is_low() -> None:
    apps = [
        app(i, name="Unrelated Thing", seller=f"Indie {i}", ratings=3, stars=3.0, days_old=900)
        for i in range(4)
    ]
    result = comp.score(serp(apps), now=NOW)
    assert result.score is not None and result.score < 25


def test_an_empty_serp_is_unscorable_rather_than_free() -> None:
    """An empty result set used to score 0 via breadth, the only weighted
    component it could compute. Calibration dropped breadth to weight 0, so
    nothing weighted remains and the honest answer became None.

    That is the better answer anyway: an empty SERP looks exactly like a
    keyword Apple has no index for, and "uncontested" is a strong claim to make
    from zero observations.
    """
    result = comp.score(serp([], count=0), now=NOW)
    assert result.sample_size == 0
    assert result.components.comp_breadth == pytest.approx(0.0)
    assert result.components.comp_rating_count is None
    assert result.score is None


def test_fewer_than_ten_results_scores_on_what_is_there() -> None:
    result = comp.score(serp([app(1), app(2), app(3)]), now=NOW)
    assert result.sample_size == 3
    assert result.score is not None


def test_only_the_top_ten_feed_the_components() -> None:
    strong = [app(i, ratings=1_000_000) for i in range(10)]
    weak = [app(i + 100, ratings=1) for i in range(40)]
    result = comp.score(serp(strong + weak, count=50), now=NOW)
    assert result.sample_size == 10
    assert result.components.comp_rating_count == pytest.approx(100.0)


def test_all_null_ratings_do_not_crash_and_leave_gaps() -> None:
    apps = [AppRecord(track_id=i) for i in range(10)]
    result = comp.score(serp(apps, count=10), now=NOW)
    assert result.components.comp_rating_count is None
    assert result.components.comp_stars is None
    assert result.components.comp_recency is None
    assert result.components.comp_publisher == pytest.approx(0.0)
    assert result.score is not None


def test_single_character_keyword_is_scored_not_rejected() -> None:
    apps = [app(1, name="X Trader"), app(2, name="Nothing")]
    result = comp.score(serp(apps, keyword="x"), now=NOW)
    assert result.components.comp_exact_match == pytest.approx(50.0)
    assert result.score is not None


def test_empty_keyword_leaves_exact_match_unknown() -> None:
    result = comp.score(serp([app(1)], keyword="   "), now=NOW)
    assert result.components.comp_exact_match is None
    assert result.score is not None


def test_every_component_stays_within_bounds() -> None:
    result = comp.score(serp([app(i) for i in range(10)], count=50), now=NOW)
    for name, value in result.components.as_dict().items():
        assert value is None or 0.0 <= value <= 100.0, name
    assert result.score is not None and 0.0 <= result.score <= 100.0


def test_component_names_match_the_snapshot_columns() -> None:
    assert set(comp.CompetitionComponents().as_dict()) == set(comp.COMPETITION_WEIGHTS)


# --- against the real captured response ------------------------------------


def test_scores_the_real_captured_serp() -> None:
    body = (FIXTURES / "itunes_search_candlestick_us.json").read_text(encoding="utf-8")
    real = itunes.parse_search_response(
        body, "candlestick patterns", "us", captured_at="t", from_cache=False
    )
    # Two of the fixture's top 10 chart in Finance; the rest genuinely do not.
    index = ChartIndex(
        country="us",
        ranks={1245684460: 1, 6739166773: 3},
        charts_loaded=48,
    )
    result = comp.score(real, now=NOW, charts=index)
    assert result.sample_size == 10
    assert result.score is not None and 0.0 < result.score < 100.0
    # Every component is computable from a real response plus a chart index.
    assert all(v is not None for v in result.components.as_dict().values())
    # 4 of the top 10 carry the full phrase "candlestick patterns" in their
    # title (4 x 1.0) and 5 more carry one of the two tokens (5 x 0.375).
    # Pinned rather than bounded: if the parser or the matcher regresses, this
    # is the number that moves. Under the old whole-phrase rule this was 40.0,
    # which ignored five apps that are plainly competing for the term.
    assert result.components.comp_exact_match == pytest.approx(58.75)
    # A niche of small apps: low review mass, but well rated and maintained.
    assert result.components.comp_rating_count < 20.0
    assert result.components.comp_stars > 60.0


def test_the_real_serp_scores_without_a_chart_index() -> None:
    """Charts are an enrichment, never a dependency.

    `aso check` and any caller that has not paid for the 48-request index must
    still get a competition score; `combine()` renormalizes around the missing
    component rather than scoring it 0.
    """
    body = (FIXTURES / "itunes_search_candlestick_us.json").read_text(encoding="utf-8")
    real = itunes.parse_search_response(
        body, "candlestick patterns", "us", captured_at="t", from_cache=False
    )
    result = comp.score(real, now=NOW)
    assert result.components.comp_app_power is None
    assert result.score is not None and 0.0 < result.score < 100.0


# --- power-mean aggregation ------------------------------------------------


def test_identical_components_are_unchanged_by_the_power_mean() -> None:
    """The power mean of a constant is that constant, at any exponent."""
    components = {name: 70.0 for name in comp.COMPETITION_WEIGHTS}
    assert comp.combine(components) == pytest.approx(70.0)


def test_a_hard_keyword_is_not_averaged_down_by_one_soft_component() -> None:
    """The reason for the exponent: you must clear every barrier, not the mean one.

    A top 10 of giants whose titles ignore the query is hard — the giants are
    still there. A plain mean calls it middling.
    """
    components = {name: 95.0 for name in comp.COMPETITION_WEIGHTS}
    components["comp_publisher"] = 0.0
    components["comp_exact_match"] = 5.0
    scored = comp.combine(components)
    plain_mean = sum(
        components[n] * w for n, w in comp.COMPETITION_WEIGHTS.items() if w > 0
    )
    assert scored is not None and scored > plain_mean


def test_the_exponent_never_leaves_the_zero_to_one_hundred_range() -> None:
    floor = {name: 0.0 for name in comp.COMPETITION_WEIGHTS}
    ceiling = {name: 100.0 for name in comp.COMPETITION_WEIGHTS}
    assert comp.combine(floor) == pytest.approx(0.0)
    assert comp.combine(ceiling) == pytest.approx(100.0)


def test_an_exponent_of_one_recovers_the_plain_weighted_mean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dial is documented as safe to turn back down to 1.0."""
    monkeypatch.setattr(comp, "COMPETITION_AGGREGATION_POWER", 1.0)
    components = {name: 40.0 for name in comp.COMPETITION_WEIGHTS}
    components["comp_publisher"] = 0.0
    components["comp_incumbent"] = 90.0
    expected = sum(
        components[n] * w for n, w in comp.COMPETITION_WEIGHTS.items() if w > 0
    )
    assert comp.combine(components) == pytest.approx(expected)
