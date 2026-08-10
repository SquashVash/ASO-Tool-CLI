from __future__ import annotations

import pytest

from aso import config


def test_competition_weights_sum_to_one() -> None:
    assert sum(config.COMPETITION_WEIGHTS.values()) == pytest.approx(1.0)


def test_competition_weight_keys_match_snapshot_columns() -> None:
    from aso import db

    conn = db.connect(":memory:")
    try:
        db.migrate(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(snapshots)")}
    finally:
        conn.close()
    assert set(config.COMPETITION_WEIGHTS) <= columns


def test_search_weights_sum_to_one() -> None:
    """Every component the scorer knows about, or the renormalization lies.

    `score_from_observations` divides by the weights of the components it
    actually has, so a set summing to less than 1 would silently rescale every
    score upward and still look self-consistent.
    """
    from aso.scoring.search import COMPONENT_NAMES

    weights = {
        "depth": config.SEARCH_DEPTH_WEIGHT,
        "savings": config.SEARCH_SAVINGS_WEIGHT,
        "rank": config.SEARCH_RANK_WEIGHT,
        "extensions": config.SEARCH_EXTENSIONS_WEIGHT,
        "rating_mass": config.SEARCH_RATING_MASS_WEIGHT,
    }
    assert set(weights) == set(COMPONENT_NAMES), "a component has no weight here"
    assert sum(weights.values()) == pytest.approx(1.0)


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASO_DEFAULT_COUNTRY", "DE")
    monkeypatch.setenv("ASO_RATE_LIMIT_PER_MIN", "7")
    loaded = config.Settings.from_env()
    assert loaded.default_country == "de"
    assert loaded.rate_limit_per_min == 7


def test_bad_numeric_setting_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASO_RATE_LIMIT_PER_MIN", "fifteen")
    with pytest.raises(ValueError):
        config.Settings.from_env()


def test_asa_is_unconfigured_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "ASO_ASA_CLIENT_ID",
        "ASO_ASA_TEAM_ID",
        "ASO_ASA_KEY_ID",
        "ASO_ASA_ORG_ID",
        "ASO_ASA_PRIVATE_KEY_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    assert config.Settings.from_env().asa.configured is False


def test_rating_mass_is_never_scored_on_both_sides() -> None:
    """It is demand now. Leaving it in competition would cancel against itself.

    `opportunity = search * (100 - competition)`, so a component carrying
    weight in both terms partly undoes its own effect — and the direction it
    used to have was backwards: the strongest predictor of demand was reducing
    the score of the highest-traffic keywords.
    """
    if config.SEARCH_RATING_MASS_WEIGHT > 0:
        assert config.COMPETITION_WEIGHTS["comp_rating_count"] == 0.0


def test_competition_still_has_weight_left() -> None:
    """Zeroing rating mass must not have zeroed the whole competition score."""
    assert sum(config.COMPETITION_WEIGHTS.values()) > 0
