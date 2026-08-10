"""The competition-side calibration: importer, storage, and the fit itself."""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

import pytest

from aso import importers, repository
from aso.scoring import competition as comp

APPFIGURES_CSV = """Keyword,Popularity,Competitiveness,Total
habit tracker,59,82,240
streaks,50,64,180
75 hard,40,,90
water reminder,31,12,40
"""


# --- reading the column the importer used to discard ------------------------


def write_csv(tmp_path: Path, body: str, name: str = "export.csv") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_reads_the_competitiveness_column(tmp_path: Path) -> None:
    result = importers.read_competition_csv(
        write_csv(tmp_path, APPFIGURES_CSV), source="appfigures", country="us"
    )
    assert {row.keyword: row.value for row in result.rows} == {
        "habit tracker": 82.0,
        "streaks": 64.0,
        "water reminder": 12.0,
    }
    assert all(row.source == "appfigures" for row in result.rows)


def test_a_blank_rating_is_skipped_and_reported(tmp_path: Path) -> None:
    """Absent is not easy. AppFigures leaves the cell empty for long-tail terms."""
    result = importers.read_competition_csv(
        write_csv(tmp_path, APPFIGURES_CSV), source="appfigures", country="us"
    )
    assert "75 hard" not in {row.keyword for row in result.rows}
    assert any("75 hard" in problem for problem in result.skipped)


def test_zero_difficulty_is_kept_unlike_zero_demand(tmp_path: Path) -> None:
    """A term nothing defends is the easy end of the range the fit needs.

    `read_demand_csv` drops zeros because zero demand is a reporting floor.
    Zero difficulty is a measurement.
    """
    body = "Keyword,Popularity,Competitiveness,Total\nobscure thing,5,0,2\n"
    result = importers.read_competition_csv(
        write_csv(tmp_path, body), source="appfigures", country="us"
    )
    assert [row.value for row in result.rows] == [0.0]


def test_a_demand_only_format_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    body = "keyword,impressions\nfoo,100\n"
    with pytest.raises(importers.ImportError_, match="no difficulty column"):
        importers.read_competition_csv(
            write_csv(tmp_path, body), source="impressions", country="us"
        )


def test_a_file_without_the_column_names_what_it_expected(tmp_path: Path) -> None:
    body = "Keyword,Popularity\nfoo,50\n"
    with pytest.raises(importers.ImportError_, match="competitiveness"):
        importers.read_competition_csv(
            write_csv(tmp_path, body), source="appfigures", country="us"
        )


# --- storage ---------------------------------------------------------------


def test_observations_round_trip_and_reimport_replaces(conn: sqlite3.Connection) -> None:
    rows = [
        repository.CompetitionWrite("appfigures", "ordinal_100", "Habit Tracker", "us", 82.0)
    ]
    assert repository.write_competition_observations(conn, rows) == 1
    repository.write_competition_observations(
        conn,
        [repository.CompetitionWrite("appfigures", "ordinal_100", "habit tracker", "us", 91.0)],
    )
    stored = conn.execute("SELECT keyword, value FROM competition_observations").fetchall()
    assert [(r["keyword"], r["value"]) for r in stored] == [("habit tracker", 91.0)]


def test_samples_join_only_within_a_storefront(conn: sqlite3.Connection) -> None:
    """A German rating must never calibrate a US keyword."""
    keyword_id, _ = repository.add_keyword(conn, "habit tracker", "us")
    conn.execute(
        "INSERT INTO snapshots (keyword_id, captured_at, comp_rating_count, fetch_failed) "
        "VALUES (?, '2026-08-09T00:00:00Z', 70.0, 0)",
        (keyword_id.id if hasattr(keyword_id, "id") else keyword_id,),
    )
    repository.write_competition_observations(
        conn,
        [repository.CompetitionWrite("appfigures", "ordinal_100", "habit tracker", "de", 82.0)],
    )
    assert repository.competition_calibration_samples(conn, "appfigures") == []


# --- the fit ---------------------------------------------------------------


def sample(value: float, **components: float) -> dict[str, float | None]:
    row: dict[str, float | None] = {name: None for name in comp.COMPETITION_WEIGHTS}
    row.update(components)
    row["value"] = value
    return row


def test_calibrate_refuses_a_sample_too_small_to_generalize() -> None:
    with pytest.raises(comp.NotEnoughData, match="at least"):
        comp.calibrate([sample(50.0, comp_rating_count=50.0) for _ in range(10)])


def test_calibrate_refuses_a_target_that_cannot_order_anything() -> None:
    """Fifty keywords all rated 60 contain no ordering to fit."""
    rows = [sample(60.0, comp_rating_count=float(i)) for i in range(60)]
    with pytest.raises(comp.NotEnoughData, match="distinct values"):
        comp.calibrate(rows)


def test_calibrate_recovers_a_component_that_drives_the_target() -> None:
    """The honest end-to-end check: plant the answer, see if the fit finds it."""
    rng = random.Random(11)
    rows = []
    for _ in range(120):
        driver = rng.uniform(0, 100)
        noise = rng.uniform(0, 100)
        rows.append(
            sample(
                driver,  # the target IS the driver, plus nothing
                comp_rating_count=driver,
                comp_recency=noise,
                comp_breadth=noise,
            )
        )
    fit = comp.calibrate(rows)
    assert fit.weights["comp_rating_count"] > 0.5
    assert fit.spearman > 0.95


def test_calibrate_reports_the_current_weights_as_a_baseline() -> None:
    rng = random.Random(3)
    rows = [
        sample(rng.uniform(0, 100), comp_rating_count=rng.uniform(0, 100),
               comp_recency=rng.uniform(0, 100), comp_breadth=rng.uniform(0, 100))
        for _ in range(120)
    ]
    fit = comp.calibrate(rows)
    assert -1.0 <= fit.baseline_spearman <= 1.0
    assert fit.n_samples == 120


def test_cross_validation_is_below_the_in_sample_number() -> None:
    """If nested CV ever matched in-sample, it would not be measuring anything.

    In-sample is the best of thousands of grid points scored on the data that
    chose them. On pure noise the gap should be large.
    """
    rng = random.Random(5)
    rows = [
        sample(rng.uniform(0, 100), comp_rating_count=rng.uniform(0, 100),
               comp_recency=rng.uniform(0, 100), comp_publisher=rng.uniform(0, 100))
        for _ in range(150)
    ]
    fit = comp.calibrate(rows)
    assert fit.cv_spearman is not None
    assert fit.cv_spearman < fit.spearman


def test_as_config_names_every_weight_even_the_zeroed_ones() -> None:
    """Pasting a partial dict would leave stale weights behind in config.py."""
    rng = random.Random(9)
    rows = [
        sample(rng.uniform(0, 100), comp_rating_count=rng.uniform(0, 100),
               comp_recency=rng.uniform(0, 100))
        for _ in range(120)
    ]
    emitted = comp.calibrate(rows).as_config()["COMPETITION_WEIGHTS"]
    assert set(emitted) == set(comp.COMPETITION_WEIGHTS)


def test_improved_compares_on_the_cross_validated_number() -> None:
    fit = comp.CompetitionCalibration(
        weights={"comp_rating_count": 1.0}, power=2.0, n_samples=100,
        spearman=0.9, cv_spearman=0.4, baseline_spearman=0.5, distinct_targets=40,
    )
    # In-sample beats the baseline, cross-validated does not. The honest answer
    # is "no".
    assert not fit.improved
    assert fit.margin == pytest.approx(-0.1)
