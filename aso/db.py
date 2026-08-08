"""SQLite schema, migrations, and connection helpers.

Raw `sqlite3` on purpose — the whole data model is four tables plus caches, and
being able to open `aso.db` and read it is worth more here than an ORM.

Conventions used throughout:

* Timestamps are ISO-8601 UTC strings (`2026-08-08T14:03:11Z`). They sort
  lexicographically, which is what all the trend queries rely on.
* Booleans are 0/1 integers.
* `country` is a two-letter lowercase storefront code and is a real column on
  every table that varies by storefront. Nothing defaults to "us" at the
  schema level.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
# Append-only list of (version, name, sql). Never edit a migration that has
# shipped — add a new one. `migrate()` runs everything not yet recorded in
# `schema_migrations`, each in its own transaction.

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial_schema",
        """
        -- Keywords being tracked. One row per (keyword, storefront).
        CREATE TABLE keywords (
            id          INTEGER PRIMARY KEY,
            keyword     TEXT    NOT NULL,
            country     TEXT    NOT NULL,
            -- Comma-separated free-form labels, used to group by app or theme.
            -- Stored as a string rather than a join table: this is a
            -- single-user tool and `tags LIKE '%,lcp,%'` is good enough.
            tags        TEXT    NOT NULL DEFAULT '',
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL,
            UNIQUE (keyword, country)
        );

        CREATE INDEX idx_keywords_country_active ON keywords (country, active);

        -- One row per keyword per refresh run. Component scores are stored
        -- alongside the finals so any score in this table can be recomputed
        -- from its own row: re-weighting history never needs a re-fetch.
        CREATE TABLE snapshots (
            id                  INTEGER PRIMARY KEY,
            keyword_id          INTEGER NOT NULL REFERENCES keywords (id) ON DELETE CASCADE,
            captured_at         TEXT    NOT NULL,

            search_score        REAL,
            competition_score   REAL,
            opportunity_score   REAL,

            -- competition components, each already normalized to 0-100
            comp_rating_count   REAL,
            comp_exact_match    REAL,
            comp_stars          REAL,
            comp_recency        REAL,
            comp_publisher      REAL,
            comp_breadth        REAL,

            -- raw search observations, kept so the depth->score mapping can be
            -- recalibrated against ASA data later without re-querying hints
            search_prefix_depth INTEGER,
            search_hint_rank    INTEGER,

            -- Failed-fetch marker. A run that couldn't reach Apple writes a row
            -- with fetch_failed = 1 and the reason, rather than writing nothing
            -- or writing a misleading zero score.
            fetch_failed        INTEGER NOT NULL DEFAULT 0,
            fetch_error         TEXT
        );

        CREATE INDEX idx_snapshots_keyword_captured
            ON snapshots (keyword_id, captured_at);

        -- Cached app metadata. Keyed by (track_id, country) because title,
        -- subtitle, price and rating counts all differ per storefront.
        CREATE TABLE apps (
            track_id                     INTEGER NOT NULL,
            country                      TEXT    NOT NULL,
            track_name                   TEXT,
            subtitle                     TEXT,
            seller_name                  TEXT,
            user_rating_count            INTEGER,
            average_user_rating          REAL,
            current_version_release_date TEXT,
            price                        REAL,
            genres                       TEXT,  -- JSON array
            fetched_at                   TEXT    NOT NULL,
            PRIMARY KEY (track_id, country)
        );

        -- Ranked results per keyword per capture. Answers "who moved into the
        -- top 10 last month" and backs `aso track`.
        CREATE TABLE serps (
            id          INTEGER PRIMARY KEY,
            keyword_id  INTEGER NOT NULL REFERENCES keywords (id) ON DELETE CASCADE,
            captured_at TEXT    NOT NULL,
            rank        INTEGER NOT NULL,
            track_id    INTEGER NOT NULL
        );

        CREATE INDEX idx_serps_keyword_captured ON serps (keyword_id, captured_at);
        CREATE INDEX idx_serps_track ON serps (track_id, captured_at);
        """,
    ),
]


SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def utcnow() -> str:
    """Current time as an ISO-8601 UTC string, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def parse_ts(value: str) -> datetime:
    """Inverse of `utcnow()`; also accepts plain `+00:00` offsets."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the pragmas this tool assumes everywhere.

    WAL + a generous busy timeout matter because a refresh run and the
    Streamlit dashboard routinely have the database open at the same time.
    """
    path = Path(db_path) if db_path is not None else settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Connection context manager that always closes."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit BEGIN/COMMIT, rolling back on any exception.

    Connections are opened with `isolation_level=None` (autocommit), so this is
    the only thing that groups statements — resumable refresh runs depend on
    each keyword's writes landing atomically.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def split_statements(script: str) -> list[str]:
    """Split a migration script into individual statements.

    Deliberately naive: a plain split on `;`. `executescript()` can't be used
    because it forces a COMMIT of any pending transaction, which would break
    the all-or-nothing guarantee around each migration. The constraint that
    falls out is that migration SQL must not contain a semicolon inside a
    string literal or an identifier.
    """
    return [stmt.strip() for stmt in script.split(";") if stmt.strip()]


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(SCHEMA_MIGRATIONS_DDL)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration. Returns the versions applied."""
    done = applied_versions(conn)
    applied: list[int] = []
    for version, name, sql in sorted(MIGRATIONS):
        if version in done:
            continue
        with transaction(conn):
            for statement in split_statements(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (version, name, utcnow()),
            )
        applied.append(version)
    return applied


def init_db(db_path: Path | str | None = None) -> list[int]:
    """Create the database if needed and bring it up to the latest schema."""
    with session(db_path) as conn:
        return migrate(conn)
