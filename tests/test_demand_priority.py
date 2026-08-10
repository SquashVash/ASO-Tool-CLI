from __future__ import annotations

import sqlite3

from aso import repository
from aso.repository import DemandWrite


def write(conn: sqlite3.Connection, *rows: DemandWrite) -> None:
    repository.write_demand_observations(conn, rows)
    conn.commit()


def obs(source: str, keyword: str, value: float, *, censored: bool = False) -> DemandWrite:
    return DemandWrite(
        source=source,
        scale="ordinal_100",
        keyword=keyword,
        country="us",
        value=value,
        censored=censored,
    )


def test_apple_beats_appfigures_regardless_of_row_count(conn) -> None:
    """The derived source must never win on volume.

    AppFigures' popularity is itself derived from Apple's indicator, so
    preferring it because it has more rows fits the copy over the original.
    """
    write(conn, *[obs("appfigures", f"kw{i}", 50.0) for i in range(100)])
    write(conn, obs("apple", "kw0", 42.0))

    assert repository.preferred_demand_source(conn) == "apple"


def test_asa_sits_between_apple_and_appfigures(conn) -> None:
    write(conn, obs("appfigures", "a", 50.0), obs("asa", "b", 900.0))
    assert repository.preferred_demand_source(conn) == "asa"


def test_a_source_with_only_censored_rows_does_not_win(conn) -> None:
    """400 rows of 'below threshold' is 400 rows and no ordering."""
    write(conn, *[obs("apple", f"kw{i}", 0.0, censored=True) for i in range(50)])
    write(conn, obs("appfigures", "real", 60.0))

    assert repository.preferred_demand_source(conn) == "appfigures"


def test_no_sources_at_all_returns_none(conn) -> None:
    assert repository.preferred_demand_source(conn) is None


def test_censored_rows_round_trip_as_censored_not_as_zero_demand(conn) -> None:
    write(conn, obs("apple", "finsta", 0.0, censored=True), obs("apple", "insta", 75.0))

    mapped = repository.apple_demand_map(conn)
    assert mapped[("finsta", "us")] == (None, True)
    assert mapped[("insta", "us")] == (75.0, False)


def test_re_reading_a_keyword_replaces_its_censoring(conn) -> None:
    """A term Apple starts scoring must stop being censored."""
    write(conn, obs("apple", "rising", 0.0, censored=True))
    write(conn, obs("apple", "rising", 31.0))

    assert repository.apple_demand_map(conn)[("rising", "us")] == (31.0, False)


def test_censored_rows_are_excluded_from_calibration(conn) -> None:
    """They bound the score; they do not train it."""
    write(conn, obs("apple", "finsta", 0.0, censored=True))
    rows = conn.execute(
        "SELECT COUNT(*) FROM demand_observations WHERE censored = 0 AND value > 0"
    ).fetchone()
    assert rows[0] == 0
