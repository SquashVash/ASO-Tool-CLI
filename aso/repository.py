"""Queries over the four tables.

Keeps SQL out of the CLI, the pipeline and the dashboard, so there is one place
to look when a query is wrong and one place to change when the schema moves.

Everything returns `sqlite3.Row` or plain dataclasses. Nothing here does I/O
beyond the database, and nothing here scores anything.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .config import DEMAND_SOURCE_PRIORITY
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


def keyword_footprint(conn: sqlite3.Connection, keyword_id: int) -> dict[str, int]:
    """How many rows a keyword owns, for showing what a delete would destroy.

    A confirmation that says "delete 3 keywords?" is not informed consent when
    one of them carries six months of trend history and the others carry none.
    """
    return {
        "snapshots": int(
            conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE keyword_id = ?", (keyword_id,)
            ).fetchone()[0]
        ),
        "serps": int(
            conn.execute(
                "SELECT COUNT(*) FROM serps WHERE keyword_id = ?", (keyword_id,)
            ).fetchone()[0]
        ),
    }


def delete_keyword(conn: sqlite3.Connection, keyword_id: int) -> dict[str, int]:
    """Permanently delete a keyword and everything recorded about it.

    Returns the row counts removed, so a caller can say what it destroyed
    rather than "done". Prefer `set_active(..., False)`: deactivating stops
    refreshes and drops the keyword out of calibration while keeping its
    history, and it is reversible. This is not.

    Snapshots and SERPs go via `ON DELETE CASCADE`, which is only honoured
    because `connect()` sets `PRAGMA foreign_keys = ON` — SQLite ignores the
    clause otherwise and would silently orphan them. The counts are read
    *before* the delete for exactly that reason: they would come back zero if
    the cascade were off, and reporting real numbers means a broken pragma
    shows up as a mismatch rather than as silence.

    `demand_observations` are deliberately NOT deleted. They key on the keyword
    text rather than its id, and they are measurements from a vendor export
    that remain true whether or not you track the term. They stay available to
    a future `aso calibrate` if you add the keyword back, and are inert
    meanwhile because `calibration_samples` joins through `keywords`.
    """
    snapshots = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE keyword_id = ?", (keyword_id,)
    ).fetchone()[0]
    serps = conn.execute(
        "SELECT COUNT(*) FROM serps WHERE keyword_id = ?", (keyword_id,)
    ).fetchone()[0]
    cursor = conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
    return {
        "keywords": cursor.rowcount,
        "snapshots": int(snapshots),
        "serps": int(serps),
    }


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
    competition_score_raw: float | None = None
    comp_rating_count: float | None = None
    comp_exact_match: float | None = None
    comp_stars: float | None = None
    comp_recency: float | None = None
    comp_publisher: float | None = None
    comp_breadth: float | None = None
    comp_incumbent: float | None = None
    comp_app_power: float | None = None
    search_prefix_depth: int | None = None
    search_hint_rank: int | None = None
    search_hint_extensions: int | None = None
    fetch_failed: bool = False
    fetch_error: str | None = None


def write_snapshot(conn: sqlite3.Connection, snapshot: SnapshotWrite) -> int:
    cursor = conn.execute(
        """
        INSERT INTO snapshots (
            keyword_id, captured_at,
            search_score, competition_score, opportunity_score,
            competition_score_raw,
            comp_rating_count, comp_exact_match, comp_stars,
            comp_recency, comp_publisher, comp_breadth, comp_incumbent,
            comp_app_power,
            search_prefix_depth, search_hint_rank, search_hint_extensions,
            fetch_failed, fetch_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.keyword_id,
            snapshot.captured_at,
            snapshot.search_score,
            snapshot.competition_score,
            snapshot.opportunity_score,
            snapshot.competition_score_raw,
            snapshot.comp_rating_count,
            snapshot.comp_exact_match,
            snapshot.comp_stars,
            snapshot.comp_recency,
            snapshot.comp_publisher,
            snapshot.comp_breadth,
            snapshot.comp_incumbent,
            snapshot.comp_app_power,
            snapshot.search_prefix_depth,
            snapshot.search_hint_rank,
            snapshot.search_hint_extensions,
            1 if snapshot.fetch_failed else 0,
            snapshot.fetch_error,
        ),
    )
    return int(cursor.lastrowid)


def all_snapshots_for_rescoring(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every snapshot with the stored components a re-score needs.

    Joins in `keywords.keyword` because the search mapping needs the keyword's
    length, which is the one input not duplicated onto the snapshot row, and
    `keywords.country` because the blend is per-storefront: popularity and the
    bridge fitted from it both belong to one market.
    Failed fetches are included — they have no components, so they re-score to
    the same NULLs and stay honest about having failed.
    """
    return conn.execute(
        """
        SELECT s.*,
               k.keyword AS keyword_text,
               k.country AS keyword_country
        FROM snapshots s
        JOIN keywords k ON k.id = s.keyword_id
        ORDER BY s.id ASC
        """
    ).fetchall()


def apple_demand_map(
    conn: sqlite3.Connection, *, source: str = "apple"
) -> dict[tuple[str, str], tuple[float | None, bool]]:
    """(keyword, country) -> (value, censored) for one demand source.

    Loaded once per re-score rather than queried per snapshot: a re-score walks
    every snapshot ever taken, and this table is small enough to hold entirely.
    """
    return {
        (row["keyword"], row["country"]): (
            None if row["censored"] else row["value"],
            bool(row["censored"]),
        )
        for row in conn.execute(
            "SELECT keyword, country, value, censored FROM demand_observations "
            "WHERE source = ?",
            (source,),
        ).fetchall()
    }


def update_snapshot_scores(
    conn: sqlite3.Connection,
    snapshot_id: int,
    *,
    search_score: float | None,
    competition_score: float | None,
    opportunity_score: float | None,
    search_score_proxy: float | None = None,
    search_source: str | None = None,
    competition_score_raw: float | None = None,
    comp_incumbent: float | None = None,
) -> None:
    """Overwrite only the finals. Measured components and raw observations are
    the inputs to the re-score and must never be touched by it.

    `search_score_proxy` is a final too, not an input: it is what the mapping
    computes from the stored observations, so it moves when the mapping is
    recalibrated. What stays fixed is the ladder itself. Keeping it separate
    from `search_score` is what stops a bridged score being bridged twice —
    see db.py migration 9.

    `comp_incumbent` is a final for the same reason, and it is the one column
    named `comp_*` that this function is allowed to write. It is not a
    measurement — it is `sqrt(comp_rating_count * comp_stars)`, so it moves
    whenever that formula does, while its two inputs do not. Leaving it alone
    would let the stored column disagree with the competition score computed
    from it in the same pass, which `aso show` and the dashboard would then
    display side by side.
    """
    conn.execute(
        "UPDATE snapshots SET search_score = ?, competition_score = ?, "
        "opportunity_score = ?, search_score_proxy = ?, search_source = ?, "
        "comp_incumbent = ?, competition_score_raw = ? "
        "WHERE id = ?",
        (
            search_score,
            competition_score,
            opportunity_score,
            search_score_proxy,
            search_source,
            comp_incumbent,
            competition_score_raw,
            snapshot_id,
        ),
    )


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
            s.competition_score_raw,
            s.comp_rating_count, s.comp_exact_match, s.comp_stars,
            s.comp_recency, s.comp_publisher, s.comp_breadth, s.comp_incumbent,
            s.comp_app_power,
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


# ---------------------------------------------------------------------------
# Apple Search Ads observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ASATermWrite:
    org_id: str
    campaign_id: int
    search_term: str
    country: str | None
    impressions: int
    taps: int | None
    installs: int | None
    start_date: str
    end_date: str


def write_asa_terms(conn: sqlite3.Connection, rows: Sequence[ASATermWrite]) -> int:
    """Upsert measured search terms. Returns the number of rows written.

    Re-pulling the same window updates in place rather than accumulating
    duplicates, so `aso asa pull` is safe to re-run after a partial failure.
    """
    now = utcnow()
    written = 0
    for row in rows:
        conn.execute(
            """
            INSERT INTO asa_search_terms (
                org_id, campaign_id, search_term, country,
                impressions, taps, installs, start_date, end_date, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (org_id, campaign_id, search_term, start_date, end_date)
            DO UPDATE SET
                country = excluded.country,
                impressions = excluded.impressions,
                taps = excluded.taps,
                installs = excluded.installs,
                fetched_at = excluded.fetched_at
            """,
            (
                row.org_id,
                row.campaign_id,
                normalize_keyword(row.search_term),
                row.country.lower() if row.country else None,
                row.impressions,
                row.taps,
                row.installs,
                row.start_date,
                row.end_date,
                now,
            ),
        )
        written += 1
    return written


def asa_term_totals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Impressions per (term, country), summed across campaigns and windows.

    Summing across campaigns is right — two campaigns bidding on the same term
    both saw real demand. Summing across *windows* would double-count an
    overlapping pull, which is why the unique key pins the window and repeated
    pulls of the same range update in place instead of inserting.
    """
    return conn.execute(
        """
        SELECT search_term, country,
               SUM(impressions) AS impressions,
               COUNT(*) AS row_count
        FROM asa_search_terms
        WHERE country IS NOT NULL
        GROUP BY search_term, country
        ORDER BY impressions DESC
        """
    ).fetchall()


@dataclass(frozen=True)
class DemandWrite:
    """One measured demand value for a keyword in a storefront."""

    source: str
    scale: str
    keyword: str
    country: str
    value: float
    # True when the source was asked and reported no value because the keyword
    # sits below its reporting threshold. `value` is then meaningless as a
    # level — read it as an upper bound. See db.py migration 6.
    censored: bool = False


def write_demand_observations(
    conn: sqlite3.Connection, rows: Sequence[DemandWrite]
) -> int:
    """Upsert demand observations. Re-importing a source replaces its values."""
    now = utcnow()
    written = 0
    for row in rows:
        conn.execute(
            """
            INSERT INTO demand_observations
                (source, scale, keyword, country, value, censored, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, keyword, country) DO UPDATE SET
                scale = excluded.scale,
                value = excluded.value,
                censored = excluded.censored,
                captured_at = excluded.captured_at
            """,
            (
                row.source,
                row.scale,
                normalize_keyword(row.keyword),
                row.country.lower(),
                0.0 if row.censored else float(row.value),
                1 if row.censored else 0,
                now,
            ),
        )
        written += 1
    return written


@dataclass(frozen=True)
class CompetitionWrite:
    """One vendor's difficulty rating for a keyword in a storefront.

    No `censored` flag, unlike `DemandWrite`. Demand sources withhold values
    below a reporting threshold, which is information worth keeping; the
    difficulty indexes seen so far either score a keyword or omit the row
    entirely, and an omitted row is simply absent rather than bounded.
    """

    source: str
    scale: str
    keyword: str
    country: str
    value: float


def write_competition_observations(
    conn: sqlite3.Connection, rows: Sequence[CompetitionWrite]
) -> int:
    """Upsert competition observations. Re-importing a source replaces its values."""
    now = utcnow()
    written = 0
    for row in rows:
        conn.execute(
            """
            INSERT INTO competition_observations
                (source, scale, keyword, country, value, captured_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, keyword, country) DO UPDATE SET
                scale = excluded.scale,
                value = excluded.value,
                captured_at = excluded.captured_at
            """,
            (
                row.source,
                row.scale,
                normalize_keyword(row.keyword),
                row.country.lower(),
                float(row.value),
                now,
            ),
        )
        written += 1
    return written


def competition_calibration_samples(
    conn: sqlite3.Connection, source: str
) -> list[sqlite3.Row]:
    """Join vendor difficulty to the latest stored components per keyword.

    The entire input to `scoring.competition.calibrate()`, and the mirror of
    `calibration_samples` on the demand side. Joined on (keyword, country) so a
    German rating can never calibrate a US keyword.

    Every `comp_*` column comes back including the zero-weighted ones. That is
    deliberate: the fit's job is to decide which components deserve weight, and
    handing it only the ones that currently have some would let today's weights
    decide tomorrow's. `comp_stars` earning nothing has to be a finding, not an
    assumption baked into the query.

    Failed fetches are excluded — a row with no components is not a training
    point — and so are inactive keywords, matching the demand side, where
    `active = 0` is the one lever for saying what belongs to the working set.
    """
    return conn.execute(
        """
        WITH latest AS (
            SELECT s.keyword_id,
                   s.comp_rating_count,
                   s.comp_exact_match,
                   s.comp_stars,
                   s.comp_recency,
                   s.comp_publisher,
                   s.comp_breadth,
                   s.comp_incumbent,
                   s.comp_app_power,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.keyword_id
                       ORDER BY s.captured_at DESC, s.id DESC
                   ) AS recency
            FROM snapshots s
            WHERE s.fetch_failed = 0
        )
        SELECT k.keyword,
               k.country,
               latest.comp_rating_count,
               latest.comp_exact_match,
               latest.comp_stars,
               latest.comp_recency,
               latest.comp_publisher,
               latest.comp_breadth,
               latest.comp_incumbent,
               latest.comp_app_power,
               c.value AS value,
               c.scale AS scale
        FROM latest
        JOIN keywords k ON k.id = latest.keyword_id
        JOIN competition_observations c
          ON c.keyword = k.keyword
         AND c.country = k.country
         AND c.source = ?
        WHERE latest.recency = 1
          AND k.active = 1
        ORDER BY c.value DESC
        """,
        (source,),
    ).fetchall()


def preferred_demand_source(
    conn: sqlite3.Connection, *, priority: Sequence[str] = DEMAND_SOURCE_PRIORITY
) -> str | None:
    """The highest-priority demand source that actually has uncensored rows.

    Calibration fits ONE source. Which one is not a user's problem to remember,
    and it is not a question to answer by row count either: AppFigures will
    usually have the most rows and is the weakest of the three, being a model
    of the number `apple` reports directly.

    Censored rows are excluded from the count deliberately. A source that has
    been asked about 400 keywords and scored none of them has 400 rows and no
    ordering, and picking it over a smaller source with real values would be
    choosing the emptier evidence.
    """
    counts = {
        row["source"]: row["scored"]
        for row in conn.execute(
            """
            SELECT source, SUM(CASE WHEN censored = 0 AND value > 0 THEN 1 ELSE 0 END)
                       AS scored
            FROM demand_observations
            GROUP BY source
            """
        ).fetchall()
    }
    for source in priority:
        if counts.get(source):
            return source
    return None


def demand_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Available sources with their scale and row count, richest first."""
    return conn.execute(
        """
        SELECT source, scale, COUNT(*) AS rows, MAX(captured_at) AS captured_at
        FROM demand_observations
        GROUP BY source, scale
        ORDER BY rows DESC
        """
    ).fetchall()


def calibration_samples(conn: sqlite3.Connection, source: str) -> list[sqlite3.Row]:
    """Join measured demand to the latest ladder observation per keyword.

    This is the entire input to `scoring.search.calibrate()`. The join is on
    (keyword, country) so a German measurement can never calibrate a US
    keyword.

    A NULL `prefix_depth` is now KEPT, and that is the point of the filter's
    shape. It used to require depth and rank NOT NULL, which silently dropped
    every keyword autocomplete never surfaced — 95 of 182 on the reference
    sample, i.e. the majority, and precisely the ones the extensions and
    rating-mass components exist to score. Training only on the keywords the
    ladder could already see would have fitted those components on the half of
    the data that needed them least.

    What must still be excluded is a *failed fetch*, which is why
    `fetch_failed = 0` now carries that job explicitly instead of leaning on
    the NULL checks to imply it. "Apple returned no match" and "we never got
    an answer from Apple" look identical in the depth column and mean opposite
    things; only the first is a training point.

    **Inactive keywords are excluded**, matching `refresh`, which also skips
    them by default. Without this, `active = 0` silently means "stop collecting
    new observations but keep training on the old ones" — so deactivating a
    block of over-represented keywords appears to do nothing, which is exactly
    how a 59%-tied sample survived being pruned. `active` is the one lever for
    saying which keywords are part of the working set, and calibration is part
    of that work.
    """
    return conn.execute(
        """
        WITH latest AS (
            SELECT s.keyword_id,
                   s.search_prefix_depth,
                   s.search_hint_rank,
                   s.search_hint_extensions,
                   s.comp_rating_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.keyword_id
                       ORDER BY s.captured_at DESC, s.id DESC
                   ) AS recency
            FROM snapshots s
            WHERE s.fetch_failed = 0
              AND (
                   s.search_hint_extensions IS NOT NULL
                OR s.comp_rating_count IS NOT NULL
                OR (s.search_prefix_depth IS NOT NULL
                    AND s.search_hint_rank IS NOT NULL)
              )
        )
        SELECT k.keyword,
               k.country,
               latest.search_prefix_depth    AS prefix_depth,
               latest.search_hint_rank       AS hint_rank,
               latest.search_hint_extensions AS extensions,
               latest.comp_rating_count      AS rating_mass,
               d.value                       AS value,
               d.scale                       AS scale
        FROM latest
        JOIN keywords k ON k.id = latest.keyword_id
        JOIN demand_observations d
          ON d.keyword = k.keyword
         AND d.country = k.country
         AND d.source = ?
        WHERE latest.recency = 1
          AND d.value > 0
          AND k.active = 1
        ORDER BY d.value DESC
        """,
        (source,),
    ).fetchall()


def bridge_pairs(
    conn: sqlite3.Connection, *, country: str, source: str = "apple"
) -> list[sqlite3.Row]:
    """(proxy_score, measured_value) for keywords carrying both.

    The input to `scoring.bridge.fit`. Restricted to ONE country: popularity is
    a per-storefront quantity, so a map fitted across markets would average the
    US and Japanese relationships into one that describes neither.

    Reads `search_score_proxy` rather than `search_score`, because after a
    blend has been applied the latter may already BE the measured value for
    exactly the keywords in this overlap — fitting a map from Apple's numbers
    onto Apple's numbers would return the identity and look like a perfect fit.

    Censored rows are excluded. Their value is a bound, not a level, and a
    bound cannot anchor a scale.
    """
    return conn.execute(
        """
        WITH latest AS (
            SELECT s.keyword_id,
                   s.search_score_proxy,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.keyword_id
                       ORDER BY s.captured_at DESC, s.id DESC
                   ) AS recency
            FROM snapshots s
            WHERE s.fetch_failed = 0
              AND s.search_score_proxy IS NOT NULL
        )
        SELECT k.keyword,
               latest.search_score_proxy AS proxy_score,
               d.value                   AS measured
        FROM latest
        JOIN keywords k ON k.id = latest.keyword_id
        JOIN demand_observations d
          ON d.keyword = k.keyword
         AND d.country = k.country
         AND d.source = ?
        WHERE latest.recency = 1
          AND k.country = ?
          AND d.censored = 0
          AND d.value > 0
        """,
        (source, country.lower()),
    ).fetchall()


def write_bridge(
    conn: sqlite3.Connection,
    *,
    country: str,
    source: str,
    knots_json: str,
    n_overlap: int,
    rmse: float | None,
    metric: str = "demand",
) -> int:
    """Store a fitted bridge. Never updates in place — bridges are append-only.

    A snapshot scored under one bridge and a snapshot scored under its
    replacement are on different scales, and overwriting the old row would
    destroy the only record of which was in force. `rescore` applies the newest;
    the history stays auditable.

    `metric` separates the demand bridge from the competition one. They are the
    same kind of object fitted for the same reason, and without it a vendor
    that rates both would have each overwrite the other's "newest".
    """
    cursor = conn.execute(
        """
        INSERT INTO score_bridges
            (country, source, knots, n_overlap, rmse, fitted_at, metric)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (country.lower(), source, knots_json, n_overlap, rmse, utcnow(), metric),
    )
    return int(cursor.lastrowid or 0)


def latest_bridge(
    conn: sqlite3.Connection,
    *,
    country: str,
    source: str = "apple",
    metric: str = "demand",
) -> sqlite3.Row | None:
    """The newest bridge of one metric for this storefront, or None."""
    return conn.execute(
        """
        SELECT * FROM score_bridges
        WHERE country = ? AND source = ? AND metric = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (country.lower(), source, metric),
    ).fetchone()


def score_movers(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    country: str | None = None,
    tag: str | None = None,
    active_only: bool = True,
) -> list[sqlite3.Row]:
    """Each keyword's latest scores against its scores `days` ago.

    The baseline is the most recent snapshot captured at or before the cutoff,
    not "the previous snapshot" — otherwise a keyword refreshed twice in one
    afternoon would report a week-over-week move covering four hours.

    A keyword with no snapshot old enough gets NULL deltas rather than zero.
    Zero would read as "measured, and it did not move", which is a different
    and much stronger claim than "not measured a week ago". The same applies
    when the only qualifying baseline *is* the latest snapshot.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    clauses = ["latest.rn = 1"]
    params: list[object] = [cutoff]
    if active_only:
        clauses.append("k.active = 1")
    if country:
        clauses.append("k.country = ?")
        params.append(country.strip().lower())
    if tag:
        clauses.append("(',' || k.tags || ',') LIKE ?")
        params.append(f"%,{tag.strip().lower()},%")

    return conn.execute(
        f"""
        WITH ranked AS (
            SELECT s.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.keyword_id
                       ORDER BY s.captured_at DESC, s.id DESC
                   ) AS rn
            FROM snapshots s
            WHERE s.opportunity_score IS NOT NULL
        ),
        latest AS (SELECT * FROM ranked WHERE rn = 1),
        baseline AS (
            SELECT * FROM (
                SELECT s.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY s.keyword_id
                           ORDER BY s.captured_at DESC, s.id DESC
                       ) AS brn
                FROM snapshots s
                WHERE s.opportunity_score IS NOT NULL
                  AND s.captured_at <= ?
            ) WHERE brn = 1
        )
        SELECT k.id                       AS keyword_id,
               k.keyword,
               k.country,
               k.tags,
               latest.captured_at         AS captured_at,
               latest.opportunity_score   AS opportunity_score,
               latest.search_score        AS search_score,
               latest.competition_score   AS competition_score,
               base.captured_at           AS baseline_at,
               base.opportunity_score     AS baseline_opportunity,
               base.search_score          AS baseline_search,
               base.competition_score     AS baseline_competition,
               CASE WHEN base.id IS NULL OR base.id = latest.id THEN NULL
                    ELSE latest.opportunity_score - base.opportunity_score END
                                          AS opportunity_delta,
               CASE WHEN base.id IS NULL OR base.id = latest.id THEN NULL
                    ELSE latest.search_score - base.search_score END
                                          AS search_delta,
               CASE WHEN base.id IS NULL OR base.id = latest.id THEN NULL
                    ELSE latest.competition_score - base.competition_score END
                                          AS competition_delta
        FROM latest
        JOIN keywords k ON k.id = latest.keyword_id
        LEFT JOIN baseline base ON base.keyword_id = latest.keyword_id
        WHERE {' AND '.join(clauses)}
        ORDER BY ABS(COALESCE(opportunity_delta, -1)) DESC, k.keyword ASC
        """,
        params,
    ).fetchall()
