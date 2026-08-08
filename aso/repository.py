"""Queries over the four tables.

Keeps SQL out of the CLI, the pipeline and the dashboard, so there is one place
to look when a query is wrong and one place to change when the schema moves.

Everything returns `sqlite3.Row` or plain dataclasses. Nothing here does I/O
beyond the database, and nothing here scores anything.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .db import utcnow

# Sort keys accepted by `latest_scores`, mapped to SQL. Restricted to a fixed
# set because these are interpolated into the query, not bound as parameters.
SORT_COLUMNS = {
    "opportunity": "s.opportunity_score",
    "search": "s.search_score",
    "competition": "s.competition_score",
    "keyword": "k.keyword",
    "captured": "s.captured_at",
}


class UnknownKeyword(LookupError):
    def __init__(self, keyword: str, country: str) -> None:
        self.keyword = keyword
        self.country = country
        super().__init__(f"No tracked keyword {keyword!r} in storefront {country!r}")


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------


def normalize_tags(tags: Iterable[str] | str | None) -> str:
    """Canonical comma-separated tag string: lowercase, deduped, sorted.

    Sorting makes the stored value stable so two identical tag sets compare
    equal, which matters for the CSV import's "did this change?" check.
    """
    if tags is None:
        return ""
    raw = tags.split(",") if isinstance(tags, str) else list(tags)
    cleaned = {tag.strip().lower() for tag in raw if tag and tag.strip()}
    return ",".join(sorted(cleaned))


def split_tags(tags: str | None) -> list[str]:
    return [tag for tag in (tags or "").split(",") if tag]


def normalize_keyword(keyword: str) -> str:
    """Canonical form for storage and lookup: lowercased, whitespace collapsed.

    App Store search is case-insensitive, so "Day Trading" and "day trading"
    are one keyword. Storing them case-preserved would let the UNIQUE
    constraint admit both and silently double the refresh cost while splitting
    one keyword's history across two rows.
    """
    return " ".join(keyword.split()).lower()


# ---------------------------------------------------------------------------
# keywords
# ---------------------------------------------------------------------------


def add_keyword(
    conn: sqlite3.Connection,
    keyword: str,
    country: str,
    tags: Iterable[str] | str | None = None,
) -> tuple[int, bool]:
    """Insert a keyword, or merge tags into the existing one.

    Returns `(keyword_id, created)`. Re-adding an existing keyword with a new
    tag adds the tag rather than erroring or replacing the tag set — importing
    an overlapping CSV should be safe to repeat.
    """
    keyword = normalize_keyword(keyword)
    country = country.strip().lower()
    if not keyword:
        raise ValueError("keyword cannot be empty")
    if not country:
        raise ValueError("country cannot be empty")

    existing = get_keyword(conn, keyword, country)
    if existing is not None:
        merged = normalize_tags(split_tags(existing["tags"]) + split_tags(normalize_tags(tags)))
        if merged != (existing["tags"] or ""):
            conn.execute(
                "UPDATE keywords SET tags = ? WHERE id = ?", (merged, existing["id"])
            )
        return existing["id"], False

    cursor = conn.execute(
        "INSERT INTO keywords (keyword, country, tags, active, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        (keyword, country, normalize_tags(tags), utcnow()),
    )
    return int(cursor.lastrowid), True


def get_keyword(
    conn: sqlite3.Connection, keyword: str, country: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM keywords WHERE keyword = ? AND country = ?",
        (normalize_keyword(keyword), country.strip().lower()),
    ).fetchone()


def require_keyword(
    conn: sqlite3.Connection, keyword: str, country: str
) -> sqlite3.Row:
    row = get_keyword(conn, keyword, country)
    if row is None:
        raise UnknownKeyword(keyword, country)
    return row


def list_keywords(
    conn: sqlite3.Connection,
    *,
    tag: str | None = None,
    country: str | None = None,
    active_only: bool = True,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if active_only:
        clauses.append("active = 1")
    if country:
        clauses.append("country = ?")
        params.append(country.strip().lower())
    if tag:
        # Wrap both sides in commas so 'lcp' never matches 'lcp-old'.
        clauses.append("(',' || tags || ',') LIKE ?")
        params.append(f"%,{tag.strip().lower()},%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM keywords {where} ORDER BY keyword, country", params
    ).fetchall()


def set_active(conn: sqlite3.Connection, keyword_id: int, active: bool) -> None:
    conn.execute(
        "UPDATE keywords SET active = ? WHERE id = ?", (1 if active else 0, keyword_id)
    )


def all_tags(conn: sqlite3.Connection) -> list[str]:
    seen: set[str] = set()
    for row in conn.execute("SELECT tags FROM keywords"):
        seen.update(split_tags(row["tags"]))
    return sorted(seen)


def countries(conn: sqlite3.Connection) -> list[str]:
    return [
        row["country"]
        for row in conn.execute(
            "SELECT DISTINCT country FROM keywords ORDER BY country"
        )
    ]


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotWrite:
    """Everything one refresh learned about one keyword.

    Component values are written alongside the finals, never instead of them:
    every score in the database has to be recomputable from its own row.
    """

    keyword_id: int
    captured_at: str
    search_score: float | None = None
    competition_score: float | None = None
    opportunity_score: float | None = None
    comp_rating_count: float | None = None
    comp_exact_match: float | None = None
    comp_stars: float | None = None
    comp_recency: float | None = None
    comp_publisher: float | None = None
    comp_breadth: float | None = None
    search_prefix_depth: int | None = None
    search_hint_rank: int | None = None
    fetch_failed: bool = False
    fetch_error: str | None = None


def write_snapshot(conn: sqlite3.Connection, snapshot: SnapshotWrite) -> int:
    cursor = conn.execute(
        """
        INSERT INTO snapshots (
            keyword_id, captured_at,
            search_score, competition_score, opportunity_score,
            comp_rating_count, comp_exact_match, comp_stars,
            comp_recency, comp_publisher, comp_breadth,
            search_prefix_depth, search_hint_rank,
            fetch_failed, fetch_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.keyword_id,
            snapshot.captured_at,
            snapshot.search_score,
            snapshot.competition_score,
            snapshot.opportunity_score,
            snapshot.comp_rating_count,
            snapshot.comp_exact_match,
            snapshot.comp_stars,
            snapshot.comp_recency,
            snapshot.comp_publisher,
            snapshot.comp_breadth,
            snapshot.search_prefix_depth,
            snapshot.search_hint_rank,
            1 if snapshot.fetch_failed else 0,
            snapshot.fetch_error,
        ),
    )
    return int(cursor.lastrowid)


def latest_snapshot(conn: sqlite3.Connection, keyword_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshots WHERE keyword_id = ? "
        "ORDER BY captured_at DESC, id DESC LIMIT 1",
        (keyword_id,),
    ).fetchone()


def snapshot_history(
    conn: sqlite3.Connection, keyword_id: int, *, limit: int | None = None
) -> list[sqlite3.Row]:
    """Oldest first, which is the order trend charts want."""
    sql = "SELECT * FROM snapshots WHERE keyword_id = ? ORDER BY captured_at ASC, id ASC"
    if limit is not None:
        # Take the newest `limit` rows, then restore chronological order.
        sql = (
            "SELECT * FROM (SELECT * FROM snapshots WHERE keyword_id = ? "
            "ORDER BY captured_at DESC, id DESC LIMIT ?) ORDER BY captured_at ASC, id ASC"
        )
        return conn.execute(sql, (keyword_id, limit)).fetchall()
    return conn.execute(sql, (keyword_id,)).fetchall()


def latest_scores(
    conn: sqlite3.Connection,
    *,
    tag: str | None = None,
    country: str | None = None,
    sort: str = "opportunity",
    limit: int | None = None,
    active_only: bool = True,
    include_unscored: bool = True,
) -> list[sqlite3.Row]:
    """Every keyword with its most recent snapshot, sorted.

    `include_unscored` keeps keywords that have never been refreshed, so a
    freshly imported set doesn't silently vanish from `aso list`.
    """
    if sort not in SORT_COLUMNS:
        raise ValueError(f"Unknown sort {sort!r}. Choose from: {', '.join(SORT_COLUMNS)}")

    clauses: list[str] = []
    params: list[object] = []
    if active_only:
        clauses.append("k.active = 1")
    if country:
        clauses.append("k.country = ?")
        params.append(country.strip().lower())
    if tag:
        clauses.append("(',' || k.tags || ',') LIKE ?")
        params.append(f"%,{tag.strip().lower()},%")
    if not include_unscored:
        clauses.append("s.id IS NOT NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    column = SORT_COLUMNS[sort]
    direction = "ASC" if sort == "keyword" else "DESC"
    # NULLs last regardless of direction: an unscored keyword is not a
    # high-opportunity one, and must never head the default sort.
    order = f"({column} IS NULL) ASC, {column} {direction}, k.keyword ASC"

    sql = f"""
        SELECT
            k.id AS keyword_id, k.keyword, k.country, k.tags, k.active,
            s.captured_at, s.search_score, s.competition_score, s.opportunity_score,
            s.comp_rating_count, s.comp_exact_match, s.comp_stars,
            s.comp_recency, s.comp_publisher, s.comp_breadth,
            s.search_prefix_depth, s.search_hint_rank,
            s.fetch_failed, s.fetch_error
        FROM keywords k
        LEFT JOIN snapshots s ON s.id = (
            SELECT id FROM snapshots
            WHERE keyword_id = k.id
            ORDER BY captured_at DESC, id DESC
            LIMIT 1
        )
        {where}
        ORDER BY {order}
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# serps
# ---------------------------------------------------------------------------


def write_serp(
    conn: sqlite3.Connection,
    keyword_id: int,
    captured_at: str,
    track_ids: Sequence[int],
) -> None:
    """Replace the ranking for one (keyword, capture).

    Replaces rather than appends so re-running a refresh against a cached SERP
    — same `captured_at` — doesn't duplicate the ranking.
    """
    conn.execute(
        "DELETE FROM serps WHERE keyword_id = ? AND captured_at = ?",
        (keyword_id, captured_at),
    )
    conn.executemany(
        "INSERT INTO serps (keyword_id, captured_at, rank, track_id) VALUES (?, ?, ?, ?)",
        [(keyword_id, captured_at, rank, tid) for rank, tid in enumerate(track_ids, 1)],
    )


def latest_serp(
    conn: sqlite3.Connection, keyword_id: int, *, limit: int = 10
) -> list[sqlite3.Row]:
    """The most recent ranking, joined to cached app metadata."""
    return conn.execute(
        """
        SELECT s.rank, s.track_id, s.captured_at,
               a.track_name, a.seller_name, a.user_rating_count,
               a.average_user_rating, a.current_version_release_date
        FROM serps s
        JOIN keywords k ON k.id = s.keyword_id
        LEFT JOIN apps a ON a.track_id = s.track_id AND a.country = k.country
        WHERE s.keyword_id = ?
          AND s.captured_at = (SELECT MAX(captured_at) FROM serps WHERE keyword_id = ?)
        ORDER BY s.rank ASC
        LIMIT ?
        """,
        (keyword_id, keyword_id, limit),
    ).fetchall()


def track_positions(
    conn: sqlite3.Connection, track_id: int, *, country: str | None = None
) -> list[sqlite3.Row]:
    """Where one app sits across every tracked keyword, latest vs previous.

    `previous_rank` is NULL if there's only one capture. A row with a NULL
    `rank` but a non-NULL `previous_rank` means the app dropped out of the
    stored ranking entirely — which is exactly the movement worth seeing.
    """
    params: list[object] = [track_id]
    country_clause = ""
    if country:
        country_clause = "AND k.country = ?"
        params.append(country.strip().lower())

    return conn.execute(
        f"""
        WITH captures AS (
            SELECT keyword_id, captured_at,
                   DENSE_RANK() OVER (
                       PARTITION BY keyword_id ORDER BY captured_at DESC
                   ) AS recency
            FROM (SELECT DISTINCT keyword_id, captured_at FROM serps)
        )
        SELECT
            k.id AS keyword_id, k.keyword, k.country, k.tags,
            MAX(CASE WHEN c.recency = 1 THEN s.rank END) AS rank,
            MAX(CASE WHEN c.recency = 2 THEN s.rank END) AS previous_rank,
            MAX(CASE WHEN c.recency = 1 THEN c.captured_at END) AS captured_at
        FROM captures c
        JOIN keywords k ON k.id = c.keyword_id
        LEFT JOIN serps s
            ON s.keyword_id = c.keyword_id
           AND s.captured_at = c.captured_at
           AND s.track_id = ?
        WHERE c.recency <= 2 {country_clause}
        GROUP BY k.id
        HAVING rank IS NOT NULL OR previous_rank IS NOT NULL
        ORDER BY (rank IS NULL) ASC, rank ASC, k.keyword ASC
        """,
        params,
    ).fetchall()
