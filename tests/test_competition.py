from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from aso.clients import itunes
from aso.clients.itunes import AppRecord, Serp
from aso.scoring import competition as comp

from .conftest import FIXTURES

NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


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
    expected = min(math.log10(1001) / 6, 1) * 100
    assert comp.rating_count_component(apps) == pytest.approx(expected)


def test_rating_count_saturates_at_a_million() -> None:
    assert comp.rating_count_component([app(ratings=10_000_000)]) == pytest.approx(100.0)


def test_rating_count_uses_the_median_not_the_mean() -> None:
    """One 5M-rating giant among nine tiny apps must not dominate."""
    apps = [app(i, ratings=10) for i in range(9)] + [app(99, ratings=5_000_000)]
    assert comp.rating_count_component(apps) == pytest.approx(
        min(math.log10(11) / 6, 1) * 100
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


def test_exact_match_requires_the_whole_phrase() -> None:
    apps = [app(name="Trading Day Planner")]
    assert comp.exact_match_component(apps, "day trading") == pytest.approx(0.0)


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


def test_stars_are_the_median_out_of_five() -> None:
    apps = [app(1, stars=4.0), app(2, stars=5.0), app(3, stars=3.0)]
    assert comp.stars_component(apps) == pytest.approx(80.0)


def test_unrated_apps_are_excluded_from_the_stars_median() -> None:
    """Apple reports averageUserRating 0.0 for unrated apps.

    That is missing data, not a zero-star app. Counting it would halve the
    quality signal of a niche full of unrated entrants.
    """
    apps = [app(1, stars=4.5, ratings=500), app(2, stars=0.0, ratings=0)]
    assert comp.stars_component(apps) == pytest.approx(90.0)


def test_a_genuine_low_rating_still_counts() -> None:
    apps = [app(1, stars=1.0, ratings=25)]
    assert comp.stars_component(apps) == pytest.approx(20.0)


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


def test_publisher_counts_repeat_sellers_across_the_full_result_set() -> None:
    top = [app(1, seller="Acme"), app(2, seller="Solo")]
    # Acme appears twice overall; the second appearance is outside the top 10.
    full = top + [app(3, seller="Acme")]
    assert comp.publisher_component(top, full) == pytest.approx(50.0)


def test_a_seller_appearing_once_does_not_count() -> None:
    top = [app(1, seller="Solo"), app(2, seller="Other")]
    assert comp.publisher_component(top, top) == pytest.approx(0.0)


def test_one_publisher_owning_the_space_scores_100() -> None:
    top = [app(i, seller="Acme") for i in range(10)]
    assert comp.publisher_component(top, top) == pytest.approx(100.0)


def test_seller_matching_ignores_case_and_spacing() -> None:
    top = [app(1, seller="Acme  Inc"), app(2, seller="ACME INC")]
    assert comp.publisher_component(top, top) == pytest.approx(100.0)


def test_unknown_sellers_never_count_as_concentration() -> None:
    top = [app(1, seller=None), app(2, seller=None)]
    assert comp.publisher_component(top, top) == pytest.approx(0.0)


def test_publisher_is_none_for_an_empty_top_set() -> None:
    assert comp.publisher_component([], []) is None


# --- comp_breadth ----------------------------------------------------------


def test_breadth_matches_the_documented_formula() -> None:
    assert comp.breadth_component(43) == pytest.approx(min(math.log10(44) / 2.3, 1) * 100)


def test_breadth_is_zero_for_no_results() -> None:
    assert comp.breadth_component(0) == pytest.approx(0.0)


def test_breadth_saturates() -> None:
    assert comp.breadth_component(10_000) == pytest.approx(100.0)


# --- combine ---------------------------------------------------------------


def test_combine_is_a_plain_weighted_mean_when_all_present() -> None:
    components = {name: 50.0 for name in comp.COMPETITION_WEIGHTS}
    assert comp.combine(components) == pytest.approx(50.0)


def test_combine_renormalizes_around_missing_components() -> None:
    """A missing component must not drag the score toward zero."""
    components = {name: 80.0 for name in comp.COMPETITION_WEIGHTS}
    components["comp_stars"] = None
    components["comp_recency"] = None
    assert comp.combine(components) == pytest.approx(80.0)


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


def test_empty_serp_scores_zero_on_breadth_alone() -> None:
    result = comp.score(serp([], count=0), now=NOW)
    assert result.sample_size == 0
    assert result.components.comp_breadth == pytest.approx(0.0)
    assert result.components.comp_rating_count is None
    assert result.components.comp_stars is None
    assert result.score == pytest.approx(0.0)


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
    result = comp.score(real, now=NOW)
    assert result.sample_size == 10
    assert result.score is not None and 0.0 < result.score < 100.0
    # Every component is computable from a real response.
    assert all(v is not None for v in result.components.as_dict().values())
    # Exactly 4 of the top 10 carry the full phrase "candlestick patterns" in
    # their title. Pinned rather than bounded: if the parser or the matcher
    # regresses, this is the number that moves.
    assert result.components.comp_exact_match == pytest.approx(40.0)
    # A niche of small apps: low review mass, but well rated and maintained.
    assert result.components.comp_rating_count < 20.0
    assert result.components.comp_stars > 80.0
