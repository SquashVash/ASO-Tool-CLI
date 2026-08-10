from __future__ import annotations

import pytest

from aso.clients.apple_popularity import (
    ApplePopularityError,
    normalize_term,
    parse_recommendations,
)
from aso.config import APPLE_POPULARITY_CEILING, APPLE_POPULARITY_FLOOR


def envelope(*pairs: tuple[str, object]) -> dict:
    """The captured response shape, verbatim."""
    return {
        "data": {
            "recommendationV2": {
                "getRecommendedKeywords": [
                    {"name": name, "popularity": pop} for name, pop in pairs
                ]
            }
        }
    }


def test_parses_the_captured_envelope() -> None:
    rows = parse_recommendations(envelope(("instagram", 99), ("insta", 73)))
    assert rows == [("instagram", 99.0), ("insta", 73.0)]


def test_a_term_apple_scores_low_is_still_a_measurement() -> None:
    """Apple's floor is 5, and 5 means 5 — not 'missing'."""
    rows = parse_recommendations(envelope(("# instagram", 5)))
    assert rows == [("# instagram", APPLE_POPULARITY_FLOOR)]


def test_values_outside_apples_band_are_clamped() -> None:
    rows = dict(parse_recommendations(envelope(("a", 5000), ("b", 2))))
    assert rows["a"] == pytest.approx(APPLE_POPULARITY_CEILING)
    assert rows["b"] == pytest.approx(APPLE_POPULARITY_FLOOR)


def test_a_null_popularity_reads_as_no_value() -> None:
    assert parse_recommendations(envelope(("x", None))) == [("x", None)]


def test_graphql_errors_raise_rather_than_reading_as_no_keywords() -> None:
    """The failure that would otherwise censor every keyword in a run.

    An empty list and an error block both yield 'no results'. Treating the
    second as the first writes a below-threshold observation for every keyword
    asked about, which is a database full of invented measurements.
    """
    with pytest.raises(ApplePopularityError, match="GraphQL error"):
        parse_recommendations({"errors": [{"message": "Your query doesn't match"}]})


def test_a_login_page_raises_rather_than_returning_empty() -> None:
    with pytest.raises(ApplePopularityError, match="no.*getRecommendedKeywords"):
        parse_recommendations({"data": {}})


def test_a_non_object_body_raises() -> None:
    with pytest.raises(ApplePopularityError, match="not a JSON object"):
        parse_recommendations("<html>sign in</html>")


def test_rows_without_a_usable_name_are_dropped() -> None:
    rows = parse_recommendations(envelope((" ", 40), ("real", 20)))
    assert rows == [("real", 20.0)]


def test_an_empty_result_list_is_valid_and_means_nothing_matched() -> None:
    assert parse_recommendations(envelope()) == []


def test_normalize_folds_composed_and_decomposed_forms_together() -> None:
    """`i̇nstagram` is in the tracked set with a combining dot.

    Without NFC folding it would never match its own result row and would be
    recorded as censored — a false measurement, which is worse than none.
    """
    composed = "i̇nstagram"
    assert normalize_term(composed) == normalize_term(
        __import__("unicodedata").normalize("NFC", composed)
    )


def test_normalize_collapses_whitespace_and_case() -> None:
    assert normalize_term("  Habit   Tracker ") == "habit tracker"
