from __future__ import annotations

from pathlib import Path

import pytest

from aso import importers
from aso.scoring.search import SCALE_COUNT, SCALE_ORDINAL_100

# The exact header AppFigures writes, captured from a real export
# (related_keywords_75_hard-ios-handheld-us-2026_08_08.csv).
APPFIGURES_HEADER = "Keyword,Popularity,Competitiveness,Total"


def write_csv(tmp_path: Path, body: str, name: str = "export.csv") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def appfigures_csv(tmp_path: Path, *rows: str) -> Path:
    return write_csv(tmp_path, "\n".join([APPFIGURES_HEADER, *rows]) + "\n")


# --- the real format -------------------------------------------------------


def test_reads_a_real_appfigures_export(tmp_path: Path) -> None:
    path = appfigures_csv(
        tmp_path,
        "habit tracker,59,76,250",
        "streaks,50,85,250",
        "75 hard challenge,40,53,249",
    )
    result = importers.read_demand_csv(path, source="appfigures", country="us")

    assert result.count == 3
    assert [r.keyword for r in result.rows] == [
        "habit tracker", "streaks", "75 hard challenge"
    ]
    assert [r.value for r in result.rows] == [59.0, 50.0, 40.0]


def test_popularity_is_recorded_as_ordinal_not_a_count(tmp_path: Path) -> None:
    """The whole point of the scale field: log10 of a 0-100 rank would distort it."""
    path = appfigures_csv(tmp_path, "habit tracker,59,76,250")
    row = importers.read_demand_csv(path, source="appfigures", country="us").rows[0]
    assert row.scale == SCALE_ORDINAL_100
    assert row.source == "appfigures"


def test_impressions_format_is_recorded_as_a_count(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "keyword,impressions\nforex,5200\n")
    row = importers.read_demand_csv(path, source="impressions", country="us").rows[0]
    assert row.scale == SCALE_COUNT


def test_country_comes_from_the_flag_because_the_file_has_none(tmp_path: Path) -> None:
    """AppFigures puts the storefront in the filename, which we refuse to parse."""
    path = appfigures_csv(tmp_path, "habit tracker,59,76,250")
    rows = importers.read_demand_csv(path, source="appfigures", country="DE").rows
    assert rows[0].country == "de"


def test_column_matching_is_case_insensitive(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "keyword,popularity\nforex,42\n")
    assert importers.read_demand_csv(path, source="appfigures", country="us").count == 1


def test_a_bom_from_excel_does_not_corrupt_the_first_column(tmp_path: Path) -> None:
    path = tmp_path / "excel.csv"
    path.write_text(f"{APPFIGURES_HEADER}\nforex,42,10,5\n", encoding="utf-8-sig")
    assert importers.read_demand_csv(path, source="appfigures", country="us").count == 1


# --- refusing to guess -----------------------------------------------------


def test_the_wrong_file_names_the_expected_columns(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "term,volume\nforex,100\n")
    with pytest.raises(importers.ImportError_) as excinfo:
        importers.read_demand_csv(path, source="appfigures", country="us")
    message = str(excinfo.value).lower()
    assert "popularity" in message, "name the column the file should have had"
    assert "term, volume" in message, "say what was actually found"


def test_an_unknown_source_is_rejected(tmp_path: Path) -> None:
    path = appfigures_csv(tmp_path, "forex,42,10,5")
    with pytest.raises(importers.ImportError_) as excinfo:
        importers.read_demand_csv(path, source="sensortower", country="us")
    assert "appfigures" in str(excinfo.value), "list what is supported"


def test_an_empty_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(importers.ImportError_):
        importers.read_demand_csv(write_csv(tmp_path, ""), source="appfigures", country="us")


def test_a_non_numeric_value_is_skipped_not_guessed(tmp_path: Path) -> None:
    """A keyword we can't read a number for is not a keyword with zero demand."""
    path = appfigures_csv(tmp_path, "forex,42,10,5", "gold,n/a,10,5")
    result = importers.read_demand_csv(path, source="appfigures", country="us")
    assert result.count == 1
    assert any("gold" in problem for problem in result.skipped)


def test_zero_demand_rows_are_dropped(tmp_path: Path) -> None:
    """They carry no gradient, and for counts they mean 'unmeasured', not 'unsearched'."""
    path = appfigures_csv(tmp_path, "forex,42,10,5", "obscure,0,1,1")
    result = importers.read_demand_csv(path, source="appfigures", country="us")
    assert [r.keyword for r in result.rows] == ["forex"]


def test_blank_keywords_are_skipped(tmp_path: Path) -> None:
    path = appfigures_csv(tmp_path, "forex,42,10,5", ",50,10,5")
    assert importers.read_demand_csv(path, source="appfigures", country="us").count == 1


def test_duplicate_keywords_keep_the_first_and_report_the_rest(tmp_path: Path) -> None:
    """The unique key would silently overwrite, hiding a bad export."""
    path = appfigures_csv(tmp_path, "forex,42,10,5", "Forex,99,10,5")
    result = importers.read_demand_csv(path, source="appfigures", country="us")
    assert result.count == 1
    assert result.rows[0].value == 42.0
    assert any("duplicate" in problem for problem in result.skipped)


def test_a_missing_file_is_reported_not_raised_raw(tmp_path: Path) -> None:
    with pytest.raises(importers.ImportError_):
        importers.read_demand_csv(tmp_path / "nope.csv", source="appfigures", country="us")


# --- stratified sampling ---------------------------------------------------


def demand(value: float, keyword: str = "k"):
    from aso.repository import DemandWrite

    return DemandWrite(
        source="appfigures", scale=SCALE_ORDINAL_100,
        keyword=f"{keyword}{value:g}", country="us", value=value,
    )


def test_stratify_spans_the_range_rather_than_taking_the_top() -> None:
    """Top-N would produce a fit that has never seen an unpopular keyword."""
    rows = [demand(float(v)) for v in range(1, 101)]
    picked = importers.stratified_sample(rows, 5)
    values = sorted(r.value for r in picked)
    assert len(picked) == 5
    assert len(set(values)) == 5, "five distinct demand levels, not five near-ties"
    assert values[-1] == 100.0, "the top of the range must be represented"


def test_stratify_avoids_piling_on_the_floor_value() -> None:
    """The failure this exists for: 21 of 26 keywords at the vendor's floor."""
    rows = [demand(5.0, f"floor{i}") for i in range(90)] + [
        demand(float(v)) for v in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
    ]
    picked = importers.stratified_sample(rows, 10)
    floor_share = sum(1 for r in picked if r.value == 5.0) / len(picked)
    assert floor_share <= 0.2, "an input that is 90% floor must not sample 90% floor"
    assert len({r.value for r in picked}) >= 9, "nearly every pick a distinct level"


def test_stratify_returns_everything_when_asked_for_more_than_exists() -> None:
    rows = [demand(float(v)) for v in range(1, 6)]
    assert len(importers.stratified_sample(rows, 50)) == 5


def test_stratify_handles_degenerate_sizes() -> None:
    rows = [demand(float(v)) for v in range(1, 11)]
    assert importers.stratified_sample(rows, 0) == []
    assert len(importers.stratified_sample(rows, 1)) == 1


def test_stratify_prefers_distinct_levels_over_more_of_the_same() -> None:
    """One from each level before a second from any."""
    rows = [demand(10.0, f"a{i}") for i in range(5)] + [demand(20.0), demand(30.0)]
    picked = importers.stratified_sample(rows, 3)
    assert sorted({r.value for r in picked}) == [10.0, 20.0, 30.0]


def test_stratify_with_a_tiny_budget_keeps_the_informative_end() -> None:
    """Too small to span everything: spend it where the ordering is real."""
    rows = [demand(5.0, f"f{i}") for i in range(50)] + [demand(90.0), demand(60.0)]
    picked = importers.stratified_sample(rows, 2)
    assert sorted(r.value for r in picked) == [60.0, 90.0]


def test_stratify_never_returns_duplicates() -> None:
    rows = [demand(float(v)) for v in range(1, 8)]
    picked = importers.stratified_sample(rows, 7)
    assert len({id(r) for r in picked}) == len(picked)
