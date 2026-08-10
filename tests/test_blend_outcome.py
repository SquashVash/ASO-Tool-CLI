from __future__ import annotations

import pytest

from aso import pipeline, repository
from aso.repository import DemandWrite
from aso.scoring.blend import SOURCE_APPLE, SOURCE_PROXY, SOURCE_PROXY_CENSORED


def outcome(keyword: str, search: float | None, competition: float | None = 50.0):
    return pipeline.KeywordOutcome(
        keyword_id=0,
        keyword=keyword,
        country="us",
        search_score=search,
        competition_score=competition,
    )


def measure(conn, keyword: str, value: float, *, censored: bool = False) -> None:
    repository.write_demand_observations(
        conn,
        [
            DemandWrite(
                source="apple",
                scale="ordinal_100",
                keyword=keyword,
                country="us",
                value=value,
                censored=censored,
            )
        ],
    )
    conn.commit()


def test_a_measured_keyword_reports_apples_number_not_the_proxy(conn) -> None:
    """The `75 hard` bug: check said 70.7 while every snapshot said 9.0."""
    measure(conn, "75 hard", 9.0)
    result = pipeline.blend_outcome(conn, outcome("75 hard", 70.74))

    assert result.search_score == pytest.approx(9.0)
    assert result.search_source == SOURCE_APPLE
    assert result.search_score_proxy == pytest.approx(70.74)


def test_opportunity_is_recomputed_from_the_blended_score(conn) -> None:
    """It multiplies by the number that just changed."""
    measure(conn, "75 hard", 9.0)
    result = pipeline.blend_outcome(conn, outcome("75 hard", 70.74, competition=50.0))

    assert result.opportunity_score == pytest.approx(9.0 * 50.0 / 100.0)


def test_an_unmeasured_keyword_keeps_the_proxy(conn) -> None:
    result = pipeline.blend_outcome(conn, outcome("never measured", 63.5))

    assert result.search_score == pytest.approx(63.5)
    assert result.search_source == SOURCE_PROXY


def test_a_censored_keyword_is_capped(conn) -> None:
    measure(conn, "finsta", 0.0, censored=True)
    result = pipeline.blend_outcome(conn, outcome("finsta", 92.3))

    assert result.search_score <= 5.0
    assert result.search_source == SOURCE_PROXY_CENSORED


def test_a_failed_fetch_stays_unscored(conn) -> None:
    """No proxy and no measurement must not become a floor score."""
    result = pipeline.blend_outcome(conn, outcome("broken", None))

    assert result.search_score is None
    assert result.opportunity_score is None


def test_measurement_is_matched_on_country(conn) -> None:
    """A German measurement must never answer for a US keyword."""
    repository.write_demand_observations(
        conn,
        [
            DemandWrite(
                source="apple",
                scale="ordinal_100",
                keyword="fitness",
                country="de",
                value=80.0,
            )
        ],
    )
    conn.commit()
    result = pipeline.blend_outcome(conn, outcome("fitness", 40.0))

    assert result.search_score == pytest.approx(40.0)
    assert result.search_source == SOURCE_PROXY
