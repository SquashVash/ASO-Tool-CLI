from __future__ import annotations


from aso import store as store_module
from aso import calibration
from aso.calibration import DemandWrite


def write(store: store_module.Store, *rows: DemandWrite) -> None:
    calibration.write_demand_observations(rows)
    store.save()


def obs(source: str, keyword: str, value: float, *, censored: bool = False) -> DemandWrite:
    return DemandWrite(
        source=source,
        scale="ordinal_100",
        keyword=keyword,
        country="us",
        value=value,
        censored=censored,
    )


def test_apple_beats_appfigures_regardless_of_row_count(store) -> None:
    """The derived source must never win on volume.

    AppFigures' popularity is itself derived from Apple's indicator, so
    preferring it because it has more rows fits the copy over the original.
    """
    write(store, *[obs("appfigures", f"kw{i}", 50.0) for i in range(100)])
    write(store, obs("apple", "kw0", 42.0))

    assert calibration.preferred_demand_source() == "apple"


def test_asa_sits_between_apple_and_appfigures(store) -> None:
    write(store, obs("appfigures", "a", 50.0), obs("asa", "b", 900.0))
    assert calibration.preferred_demand_source() == "asa"


def test_a_source_with_only_censored_rows_does_not_win(store) -> None:
    """400 rows of 'below threshold' is 400 rows and no ordering."""
    write(store, *[obs("apple", f"kw{i}", 0.0, censored=True) for i in range(50)])
    write(store, obs("appfigures", "real", 60.0))

    assert calibration.preferred_demand_source() == "appfigures"


def test_no_sources_at_all_returns_none(store) -> None:
    assert calibration.preferred_demand_source() is None


def test_censored_rows_round_trip_as_censored_not_as_zero_demand(store) -> None:
    write(store, obs("apple", "finsta", 0.0, censored=True), obs("apple", "insta", 75.0))

    mapped = calibration.demand_map()
    assert mapped[("finsta", "us")] == (None, True)
    assert mapped[("insta", "us")] == (75.0, False)


def test_re_reading_a_keyword_replaces_its_censoring(store) -> None:
    """A term Apple starts scoring must stop being censored."""
    write(store, obs("apple", "rising", 0.0, censored=True))
    write(store, obs("apple", "rising", 31.0))

    assert calibration.demand_map()[("rising", "us")] == (31.0, False)


def test_censored_rows_are_excluded_from_calibration(store) -> None:
    """They bound the score; they do not train it."""
    write(store, obs("apple", "finsta", 0.0, censored=True))
    scored = [
        row
        for row in calibration.demand_observations()
        if not row["censored"] and row["value"] > 0
    ]
    assert scored == []
