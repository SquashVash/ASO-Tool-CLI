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
    (
        2,
        "http_cache",
        """
        -- Raw response bodies, cached on disk rather than in memory so a
        -- refresh run is resumable: Ctrl-C a 500-keyword run, restart it, and
        -- everything already fetched is free.
        --
        -- Bodies are stored unparsed (JSON text for SERPs, XML plist for
        -- hints) so a parser fix can be re-run against captured responses
        -- without going back to Apple.
        CREATE TABLE http_cache (
            cache_key  TEXT NOT NULL PRIMARY KEY,
            kind       TEXT NOT NULL,  -- 'serp' | 'hints'
            body       TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );

        CREATE INDEX idx_http_cache_kind_fetched ON http_cache (kind, fetched_at);
        """,
    ),
    (
        3,
        "asa_search_terms",
        """
        -- Measured impression counts from Apple Search Ads: the only
        -- ground truth this tool ever sees. Everything else is inference.
        --
        -- Deliberately NOT in http_cache. These are observations to be kept
        -- and re-fitted against, not responses to be expired after three days.
        --
        -- `country` is the campaign's storefront, and is NULL when a campaign
        -- targets more than one — a search term cannot then be attributed to a
        -- storefront, and calibration skips those rows rather than guessing.
        --
        -- The unique key includes the window so pulling overlapping date
        -- ranges replaces rather than double-counts.
        CREATE TABLE asa_search_terms (
            id           INTEGER PRIMARY KEY,
            org_id       TEXT    NOT NULL,
            campaign_id  INTEGER NOT NULL,
            search_term  TEXT    NOT NULL,  -- normalized, lowercase
            country      TEXT,
            impressions  INTEGER NOT NULL,
            taps         INTEGER,
            installs     INTEGER,
            start_date   TEXT    NOT NULL,
            end_date     TEXT    NOT NULL,
            fetched_at   TEXT    NOT NULL,
            UNIQUE (org_id, campaign_id, search_term, start_date, end_date)
        );

        CREATE INDEX idx_asa_terms_lookup
            ON asa_search_terms (search_term, country);
        """,
    ),
    (
        4,
        "demand_observations",
        """
        -- Measured demand from any source, in one place, because calibration
        -- should not care where the ground truth came from.
        --
        -- `asa_search_terms` keeps the detailed per-campaign ASA record;
        -- this table is the flat (keyword, country) -> value view that
        -- `calibrate()` actually fits against, and ASA rows are projected
        -- into it on pull.
        --
        -- `scale` is what stops the fit being nonsense: an impression COUNT
        -- is unbounded and roughly log-distributed, while an ORDINAL score is
        -- already 0-100 and taking its log would distort the very ordering
        -- being fitted. Sources are never mixed in one fit.
        CREATE TABLE demand_observations (
            id          INTEGER PRIMARY KEY,
            source      TEXT NOT NULL,   -- 'asa' | 'appfigures' | ...
            scale       TEXT NOT NULL,   -- 'count' | 'ordinal_100'
            keyword     TEXT NOT NULL,   -- normalized, lowercase
            country     TEXT NOT NULL,
            value       REAL NOT NULL,
            captured_at TEXT NOT NULL,
            UNIQUE (source, keyword, country)
        );

        CREATE INDEX idx_demand_lookup
            ON demand_observations (source, keyword, country);
        """,
    ),
    (
        5,
        "snapshot_hint_extensions",
        """
        -- How many of the (at most 10) suggestions for the keyword's own full
        -- length prefix extend it without equalling it.
        --
        -- The ladder can only see a keyword Apple offers as a completion of
        -- something shorter, and Apple never offers the query back as its own
        -- completion. So every keyword that is a *stem* of popular longer
        -- terms reads as zero demand: 'insta' scores the same as a dead term
        -- while ten of its ten suggestions extend it. This column is the
        -- observation that separates them, and it costs no extra request --
        -- the full-length prefix is already the ladder's first rung.
        --
        -- NULL means "not measured" -- a snapshot taken before this column
        -- existed, or a failed hints fetch. 0 means "measured, nothing
        -- extends it". Those are different and the scorer treats them so.
        --
        -- Keep this comment free of semicolons. split_statements() is a naive
        -- split on the statement separator and would cut the migration in two.
        ALTER TABLE snapshots ADD COLUMN search_hint_extensions INTEGER;
        """,
    ),
    (
        6,
        "apple_popularity_and_bridge",
        """
        -- Apple reports keyword popularity on a 5-100 scale and reports
        -- NOTHING for terms below its threshold. That silence is a measurement,
        -- not a gap: it says "less popular than anything I will name". Storing
        -- it as value 0 alone would be the same conflation this schema refuses
        -- everywhere else, so the fact gets its own column.
        --
        -- censored = 1 means Apple served the term and declined to score it.
        -- The row's `value` is then meaningless as a level and is written 0 --
        -- read it as an upper bound of APPLE_POPULARITY_FLOOR, never as demand.
        -- calibration_samples already filters `value > 0`, so these rows train
        -- nothing. They are used at scoring time, where an upper bound is
        -- exactly the useful shape.
        ALTER TABLE demand_observations ADD COLUMN censored INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        7,
        "snapshot_search_source",
        """
        -- Which ruler produced `search_score` on this row.
        --
        -- 'apple'          -- Apple's own popularity index, used verbatim
        -- 'proxy'          -- the autocomplete + SERP proxy, mapped onto
        --                     Apple's scale by the fitted bridge
        -- 'proxy_censored' -- proxy, then capped at Apple's floor because
        --                     Apple was asked and declined to score the term
        --
        -- NULL means the row predates the blend and is a raw proxy score on
        -- the old, unbridged scale. That distinction matters: mixing the two
        -- in one trend chart splices two different rulers and invents movement.
        ALTER TABLE snapshots ADD COLUMN search_source TEXT;
        """,
    ),
    (
        8,
        "score_bridges",
        """
        -- The fitted monotone map from proxy score onto Apple's 5-100 scale.
        --
        -- Unlike the search-score constants, this cannot be pasted into
        -- config.py -- it is a step function with as many breakpoints as the
        -- overlap set supports. So it lives here, versioned, and `rescore`
        -- applies the newest row. `aso bridge` prints its shape and its error
        -- so it stays reviewable even though it is not hand-editable.
        --
        -- `knots` is a JSON array of [proxy_score, apple_value] pairs, sorted
        -- by proxy_score and non-decreasing in apple_value by construction.
        --
        -- The stored quality figure is RMSE, deliberately NOT Spearman. A
        -- bridge is a monotone map, so it cannot change the proxy's own rank
        -- correlation by even a rounding error -- a before/after Spearman here
        -- would print an identical pair of numbers forever and look like
        -- evidence. What the bridge fixes is the SCALE, so the honest measure
        -- is distance from Apple's values on the overlap. Whether the blend
        -- helps is a question about the blended column, and `aso eval` answers
        -- that one.
        CREATE TABLE score_bridges (
            id          INTEGER PRIMARY KEY,
            country     TEXT NOT NULL,
            source      TEXT NOT NULL,
            knots       TEXT NOT NULL,
            n_overlap   INTEGER NOT NULL,
            rmse        REAL,
            fitted_at   TEXT NOT NULL
        );

        CREATE INDEX idx_bridge_lookup ON score_bridges (country, source, id);
        """,
    ),
    (
        9,
        "snapshot_search_score_proxy",
        """
        -- The unblended autocomplete + SERP score, kept alongside the final one.
        --
        -- Without this column the blend eats its own input. `search_score`
        -- becomes Apple's value for exactly the keywords that anchor the
        -- bridge, so the next fit would map Apple's numbers onto Apple's
        -- numbers, return the identity, and report a flawless RMSE. The same
        -- collapse makes `rescore` non-idempotent -- running it twice would
        -- bridge an already-bridged score.
        --
        -- This is the same principle the schema already follows by storing
        -- every component next to the score it produced: inputs are inputs and
        -- are never overwritten by outputs.
        ALTER TABLE snapshots ADD COLUMN search_score_proxy REAL;

        -- Backfill is exact rather than approximate. Every existing
        -- `search_score` was produced by the proxy alone, because nothing else
        -- existed to produce it.
        UPDATE snapshots SET search_score_proxy = search_score
            WHERE search_score IS NOT NULL;
        """,
    ),
    (
        10,
        "snapshot_comp_incumbent",
        """
        -- sqrt(comp_rating_count * comp_stars): how strong the apps already
        -- holding the top 10 are. The heaviest competition component.
        --
        -- Stored even though it is derivable, because every other consumer --
        -- `aso show`, the dashboard's component breakdown, COMPETITION_WEIGHTS
        -- iteration in pipeline -- assumes one column per weighted component,
        -- and a single derived exception would need special-casing in each.
        --
        -- Deliberately NOT backfilled here. SQLite's sqrt() depends on the
        -- build's math extensions and is absent from stock CPython bundles, so
        -- a SQL backfill would work on some machines and fail on others -- the
        -- worst outcome for a migration. `aso rescore` recomputes this column
        -- from comp_rating_count and comp_stars for every row, which is already
        -- the documented step after a weight change, and it is what
        -- `competition.derive` exists for.
        ALTER TABLE snapshots ADD COLUMN comp_incumbent REAL;
        """,
    ),
    (
        11,
        "rescale_comp_stars_and_reset_exact_match",
        """
        -- Two competition components changed definition. Neither adds a
        -- column. Both make every stored value mean something different, and
        -- leaving them would silently mix two rulers in one table.
        --
        -- (split_statements is a naive split on the semicolon character, so
        -- nothing below may contain one except the statement terminators --
        -- comments very much included.)
        --
        -- comp_stars moved from `median / 5 * 100` to
        -- `(median - 4.0) / 1.0 * 100`, clamped. That is invertible without
        -- the raw SERP, and the arithmetic is plain +-*/ with no math
        -- extensions, so unlike migration 10's sqrt it is safe to run here:
        --
        --   median = old / 100 * 5           =>  new = old * 5 - 400
        --
        -- Rows already at 0 are left alone. A stored 0 means "median 0 stars",
        -- which stars_component never emits (unrated apps are excluded), so it
        -- can only be a genuine floor and maps to 0 either way.
        UPDATE snapshots
           SET comp_stars = MAX(0.0, MIN(100.0, comp_stars * 5.0 - 400.0))
         WHERE comp_stars IS NOT NULL;

        -- comp_exact_match went from "fraction of the top 10 whose title
        -- contains the whole keyword phrase" to a graded per-app token
        -- coverage. There is no arithmetic that recovers the new value from
        -- the old one: the old number threw away which apps matched and how
        -- nearly, and the raw titles are not stored.
        --
        -- So the old values are cleared rather than kept. This module's rule
        -- is that missing means unknown and `combine()` renormalizes around
        -- it, and a number produced by a superseded definition is exactly
        -- unknown under the current one. Keeping it would be worse than
        -- losing it: the old formula is biased low by construction, so mixed
        -- vintages would make pre-migration keywords look systematically
        -- easier than post-migration ones -- and look plausible while doing
        -- it. `aso refresh` restores the column on the next capture.
        --
        -- The `serps` + `apps` tables do hold titles, so an offline rebuild
        -- looks possible at first glance. It is not, in practice: a snapshot
        -- and its SERP rows are written with independently taken timestamps,
        -- and only 2 of 400 rows in the author's database had a captured_at
        -- that matched exactly. There is no reliable join back to the text.
        UPDATE snapshots SET comp_exact_match = NULL;
        """,
    ),
    (
        12,
        "rescale_comp_rating_count",
        """
        -- comp_rating_count's saturation point moved from a top-10 median of
        -- 1,000,000 ratings to 100,000 (COMP_RATING_COUNT_LOG_DIVISOR 6 -> 5).
        -- Almost nothing occupied the range above 100k, so the old ruler spent
        -- its top third on a region that does not exist.
        --
        -- Invertible without the raw SERP, and again plain arithmetic:
        --
        --   old = log10(median + 1) / 6 * 100
        --   new = log10(median + 1) / 5 * 100 = old * 6 / 5
        --
        -- Clamped at 100, which is where the rows that already pinned stay.
        -- (No semicolons anywhere above except the terminator below.)
        UPDATE snapshots
           SET comp_rating_count = MIN(100.0, comp_rating_count * 1.2)
         WHERE comp_rating_count IS NOT NULL;

        -- comp_incumbent is sqrt(comp_rating_count * comp_stars) and both of
        -- its inputs have now moved, so every stored value is stale. It is not
        -- recomputed here for the reason given in migration 10 -- SQLite's
        -- sqrt() is not portable -- and does not need to be: `competition.derive`
        -- rebuilds it from the two columns above on the next `aso rescore`,
        -- which is the documented step after any scoring change.
        UPDATE snapshots SET comp_incumbent = NULL;
        """,
    ),
    (
        13,
        "competition_observations",
        """
        -- Someone else's measurement of how hard a keyword is, the mirror of
        -- `demand_observations`. AppFigures ships a Competitiveness column in
        -- the same export the Popularity column comes from, and until now the
        -- importer read the one and discarded the other -- so the competition
        -- half of the model had no target to fit against and its weights were
        -- chosen by argument alone.
        --
        -- Kept in its own table rather than as a `metric` column on
        -- `demand_observations` because nothing joins the two: they fit
        -- different models, against different components, with different
        -- scales. Sharing a table would buy one fewer CREATE and cost every
        -- reader a WHERE clause they must not forget.
        --
        -- Same caveat as demand: this is a vendor's derived index, not ground
        -- truth. Fitting to it reproduces AppFigures. That is a real gain over
        -- fitting to nothing, and it is not the same as being right, so
        -- `scale` records what kind of number it is instead of letting the
        -- 0-100 formatting imply a meaning it has not got.
        CREATE TABLE competition_observations (
            id          INTEGER PRIMARY KEY,
            source      TEXT NOT NULL,   -- 'appfigures' | ...
            scale       TEXT NOT NULL,   -- 'ordinal_100'
            keyword     TEXT NOT NULL,   -- normalized, lowercase
            country     TEXT NOT NULL,
            value       REAL NOT NULL,
            captured_at TEXT NOT NULL,
            UNIQUE (source, keyword, country)
        );

        CREATE INDEX idx_competition_obs_lookup
            ON competition_observations (source, keyword, country);
        """,
    ),
    (
        14,
        "reset_breadth_and_publisher_for_wider_serp",
        """
        -- ITUNES_SEARCH_LIMIT went 50 -> 200, and `resultCount` counts what
        -- was returned, so the request limit is the ceiling on this component.
        -- Every stored value was measured against a ceiling of ~74 and is not
        -- comparable to anything captured from here on.
        --
        -- Unlike migrations 11 and 12 this is NOT invertible. The old value
        -- tells you min(log10(n+1)/2.3, 1) for an n that was itself truncated
        -- at 50: a keyword with 50 results and a keyword with 5,000 both
        -- stored ~74, and no arithmetic separates them after the fact. The
        -- truncation destroyed the measurement, not just its scale.
        --
        -- So: cleared, per the module rule that a value produced by a
        -- superseded definition is exactly unknown under the current one.
        -- Keeping it would be worse than losing it, and in the same direction
        -- migration 11 warns about -- old rows are biased toward the ceiling
        -- while new ones spread below it, so mixed vintages would make
        -- pre-migration keywords look systematically more crowded, and look
        -- plausible while doing it. `aso refresh` restores the column.
        --
        -- No cache sweep is needed. `cache.serp_key` includes the limit, so
        -- the 50-result bodies are unreachable under the new key rather than
        -- being served as if they were 200-result ones.
        UPDATE snapshots SET comp_breadth = NULL;

        -- comp_publisher moved from "fraction of the top 10 whose seller
        -- appears 2+ times in the result set" to a rank-weighted, log-graded
        -- share of the field. Not invertible either: the old value threw away
        -- which slots matched and how deep each publisher's catalogue ran.
        --
        -- It is cleared for the wider-field reason as well as the formula one.
        -- Concentration measured over 50 results and concentration measured
        -- over 200 are different quantities, and the first systematically
        -- understates: over 24 live SERPs the old component read 0 for most
        -- terms at limit 50 and for 5 of 24 at limit 200. Mixing the vintages
        -- would make pre-migration keywords look less contested than they are.
        UPDATE snapshots SET comp_publisher = NULL;
        """,
    ),
    (
        15,
        "comp_app_power",
        """
        -- How big the top 10's apps are, from Apple's published genre charts.
        -- The one thing the model could not see: every other component reads
        -- text or reputation, none of them distinguishes a category leader
        -- from a well-reviewed small app, and that distinction is most of what
        -- makes a keyword unwinnable.
        --
        -- No backfill is possible even in principle. The charts are a daily
        -- snapshot Apple does not serve history for, so a row captured in June
        -- cannot be told what was charting in June. NULL until `aso refresh`
        -- captures it alongside a fresh SERP, which is also why this component
        -- starts at weight 0 -- see COMPETITION_WEIGHTS.
        ALTER TABLE snapshots ADD COLUMN comp_app_power REAL;
        """,
    ),
    (
        16,
        "bridge_metric",
        """
        -- `score_bridges` was built when only demand had a bridge, so its
        -- identity was (country, source) and `latest_bridge` ordered by id
        -- within that pair. Competition now fits the same kind of map -- raw
        -- score onto a vendor's scale, isotonic, for the same reason -- and
        -- would collide: a demand bridge and a competition bridge fitted from
        -- the same vendor for the same storefront would each look like the
        -- other's newest row, and `rescore` would apply whichever was written
        -- last to both columns.
        --
        -- Silently, and with plausible numbers, which is why this is a column
        -- rather than a naming convention on `source`.
        --
        -- Existing rows are all demand, so the default backfills them
        -- correctly and no UPDATE is needed.
        ALTER TABLE score_bridges ADD COLUMN metric TEXT NOT NULL DEFAULT 'demand';

        DROP INDEX IF EXISTS idx_bridge_lookup;

        CREATE INDEX idx_bridge_lookup
            ON score_bridges (country, source, metric, id);
        """,
    ),
    (
        17,
        "competition_score_raw",
        """
        -- The competition score before the level bridge, the exact twin of
        -- `search_score_proxy` from migration 9 and for the same reason:
        -- keeping the pre-bridge number in its own column is what stops a
        -- bridged score being bridged again on the next `aso rescore`.
        --
        -- `competition_score` stays the column everything reads -- `opportunity`
        -- multiplies by it, the dashboard sorts on it -- so it holds the
        -- FINAL, bridged value once a bridge exists and the raw value until
        -- then. That way adding a bridge later changes no reader.
        --
        -- Backfilled from the current column rather than left NULL. Every
        -- existing row was written before any competition bridge existed, so
        -- its `competition_score` IS its raw score. That is a fact about those
        -- rows, not a guess.
        --
        -- (No semicolons above except the statement terminators -- the splitter
        -- is a naive split on the character, comments very much included.)
        ALTER TABLE snapshots ADD COLUMN competition_score_raw REAL;

        UPDATE snapshots SET competition_score_raw = competition_score;
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
