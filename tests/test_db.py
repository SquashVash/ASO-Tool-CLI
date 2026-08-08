from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aso import db


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def test_migrate_creates_every_table(conn: sqlite3.Connection) -> None:
    assert {"keywords", "snapshots", "apps", "serps"} <= table_names(conn)


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idempotent.db"
    first = db.init_db(path)
    second = db.init_db(path)
    assert first == [version for version, _, _ in db.MIGRATIONS]
    assert second == []


def test_keyword_country_pair_is_unique(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO keywords (keyword, country, tags, active, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        ("candlestick patterns", "us", "lcp", db.utcnow()),
    )
    # Same keyword in another storefront is a different row, by design.
    conn.execute(
        "INSERT INTO keywords (keyword, country, tags, active, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        ("candlestick patterns", "de", "lcp", db.utcnow()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO keywords (keyword, country, tags, active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            ("candlestick patterns", "us", "other", db.utcnow()),
        )


def test_deleting_a_keyword_cascades(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "INSERT INTO keywords (keyword, country, tags, active, created_at) "
        "VALUES ('day trading', 'us', '', 1, ?)",
        (db.utcnow(),),
    )
    keyword_id = cur.lastrowid
    conn.execute(
        "INSERT INTO snapshots (keyword_id, captured_at) VALUES (?, ?)",
        (keyword_id, db.utcnow()),
    )
    conn.execute(
        "INSERT INTO serps (keyword_id, captured_at, rank, track_id) "
        "VALUES (?, ?, 1, 627114159)",
        (keyword_id, db.utcnow()),
    )

    conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))

    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM serps").fetchone()[0] == 0


def test_snapshot_defaults_to_not_failed(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "INSERT INTO keywords (keyword, country, tags, active, created_at) "
        "VALUES ('forex', 'gb', '', 1, ?)",
        (db.utcnow(),),
    )
    conn.execute(
        "INSERT INTO snapshots (keyword_id, captured_at) VALUES (?, ?)",
        (cur.lastrowid, db.utcnow()),
    )
    row = conn.execute("SELECT * FROM snapshots").fetchone()
    assert row["fetch_failed"] == 0
    assert row["fetch_error"] is None
    # Components are nullable so a partial capture is still recordable.
    assert row["comp_rating_count"] is None


def test_apps_are_keyed_per_storefront(conn: sqlite3.Connection) -> None:
    for country in ("us", "jp"):
        conn.execute(
            "INSERT INTO apps (track_id, country, track_name, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (627114159, country, "Some App", db.utcnow()),
        )
    assert conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO apps (track_id, country, fetched_at) VALUES (?, ?, ?)",
            (627114159, "us", db.utcnow()),
        )


def test_timestamps_round_trip_as_utc() -> None:
    stamp = db.utcnow()
    assert stamp.endswith("Z")
    assert db.parse_ts(stamp).tzinfo is not None


def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO keywords (keyword, country, tags, active, created_at) "
                "VALUES ('scalping', 'us', '', 1, ?)",
                (db.utcnow(),),
            )
            # Violates the FK: no such keyword id.
            conn.execute(
                "INSERT INTO snapshots (keyword_id, captured_at) VALUES (999999, ?)",
                (db.utcnow(),),
            )
    assert conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 0
