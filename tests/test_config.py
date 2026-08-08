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
    assert config.SEARCH_DEPTH_WEIGHT + config.SEARCH_RANK_WEIGHT == pytest.approx(1.0)


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
