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


# --- migration 11: comp_stars rescale, comp_exact_match reset ---------------


def migrate_through(conn: sqlite3.Connection, last_version: int) -> None:
    """Apply migrations up to and including `last_version`, and no further."""
    conn.execute(db.SCHEMA_MIGRATIONS_DDL)
    for version, name, sql in sorted(db.MIGRATIONS):
        if version > last_version:
            return
        with db.transaction(conn):
            for statement in db.split_statements(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (version, name, db.utcnow()),
            )


def snapshot_at_version_10(
    conn: sqlite3.Connection, *, stars: float | None, exact_match: float | None
) -> None:
    cur = conn.execute(
        "INSERT INTO keywords (keyword, country, tags, active, created_at) "
        "VALUES ('smart journal', 'us', '', 1, ?)",
        (db.utcnow(),),
    )
    conn.execute(
        "INSERT INTO snapshots (keyword_id, captured_at, comp_stars, comp_exact_match) "
        "VALUES (?, ?, ?, ?)",
        (cur.lastrowid, db.utcnow(), stars, exact_match),
    )


def test_migration_11_rescales_stored_stars_onto_the_new_ruler(tmp_path: Path) -> None:
    """Old rows were `median / 5 * 100`. The new ruler anchors at 4.0 stars.

    Without this the table would hold two incompatible scales at once, and
    `aso rescore` would rank pre-migration keywords against post-migration ones
    as if the numbers meant the same thing.
    """
    with db.session(tmp_path / "m11.db") as conn:
        migrate_through(conn, 10)
        # 95.7 on the old ruler is a median of 4.785 stars.
        snapshot_at_version_10(conn, stars=95.7, exact_match=40.0)
        # Every migration after 10, not just 11 — asserting the exact list
        # would break every time a later one is appended, which is noise.
        assert 11 in db.migrate(conn)
        row = conn.execute("SELECT comp_stars FROM snapshots").fetchone()
        assert row["comp_stars"] == pytest.approx(78.5)


def test_migration_11_clamps_ratings_below_the_new_floor(tmp_path: Path) -> None:
    with db.session(tmp_path / "m11floor.db") as conn:
        migrate_through(conn, 10)
        # 60.0 old = a median of 3.0 stars, below the 4.0 anchor.
        snapshot_at_version_10(conn, stars=60.0, exact_match=None)
        db.migrate(conn)
        row = conn.execute("SELECT comp_stars FROM snapshots").fetchone()
        assert row["comp_stars"] == pytest.approx(0.0)


def test_migration_11_leaves_a_null_stars_null(tmp_path: Path) -> None:
    """Unknown must survive the rescale as unknown, not become 0."""
    with db.session(tmp_path / "m11null.db") as conn:
        migrate_through(conn, 10)
        snapshot_at_version_10(conn, stars=None, exact_match=None)
        db.migrate(conn)
        row = conn.execute("SELECT comp_stars FROM snapshots").fetchone()
        assert row["comp_stars"] is None


def test_migration_11_clears_exact_match_rather_than_keeping_a_stale_scale(
    tmp_path: Path,
) -> None:
    """The old whole-phrase value is not recoverable, and is biased low.

    Nulling it makes `combine` renormalize around it, which is this module's
    rule for unknown data. Keeping it would make pre-migration keywords look
    systematically easier than post-migration ones -- and look plausible.
    """
    with db.session(tmp_path / "m11exact.db") as conn:
        migrate_through(conn, 10)
        snapshot_at_version_10(conn, stars=95.7, exact_match=40.0)
        db.migrate(conn)
        row = conn.execute("SELECT comp_exact_match FROM snapshots").fetchone()
        assert row["comp_exact_match"] is None
