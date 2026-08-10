"""The dashboard's data path.

Streamlit widgets aren't worth simulating, but the code between the database
and the widgets is — and that is where the bugs were. The keyword-selector
indexed `frame["id"]` when `latest_scores` returns `keyword_id`, which would
have raised on the first click of the detail view.
"""

from __future__ import annotations

import ast
import sqlite3

import pandas as pd
import pytest

import dashboard
from aso import repository as repo

from .test_repository import days_ago


def scored_keyword(conn, keyword="forex", country="us", **scores):
    repo.add_keyword(conn, keyword, country)
    row = repo.require_keyword(conn, keyword, country)
    repo.write_snapshot(
        conn,
        repo.SnapshotWrite(
            keyword_id=row["id"],
            captured_at=scores.pop("captured_at", days_ago(0)),
            search_score=scores.pop("search", 60.0),
            competition_score=scores.pop("competition", 40.0),
            opportunity_score=scores.pop("opportunity", 36.0),
            comp_rating_count=50.0,
            search_prefix_depth=3,
            search_hint_rank=2,
            **scores,
        ),
    )
    return row["id"]


# --- frame conversion ------------------------------------------------------


def test_empty_rows_give_an_empty_frame_not_a_crash() -> None:
    """A fresh database is exactly this state."""
    frame = dashboard._frame([])
    assert frame.empty
    assert isinstance(frame, pd.DataFrame)


def test_rows_convert_with_their_column_names(conn: sqlite3.Connection) -> None:
    scored_keyword(conn)
    frame = dashboard._frame(repo.latest_scores(conn, sort="opportunity"))
    assert "keyword" in frame.columns
    assert frame["opportunity_score"].iloc[0] == pytest.approx(36.0)


# --- the column name that broke the detail view ----------------------------


def test_latest_scores_exposes_keyword_id_not_id(conn: sqlite3.Connection) -> None:
    """The detail view selects on this. It indexed `id` and would have raised."""
    scored_keyword(conn)
    frame = dashboard._frame(repo.latest_scores(conn, sort="opportunity"))
    assert "keyword_id" in frame.columns
    assert "id" not in frame.columns


def test_the_detail_selector_can_index_by_its_own_label(conn: sqlite3.Connection) -> None:
    """Reproduces exactly what detail_view does, minus the widgets."""
    scored_keyword(conn, "forex")
    scored_keyword(conn, "gold")
    frame = dashboard._frame(repo.latest_scores(conn, sort="opportunity"))

    labels = {
        f"{row.keyword}  ({row.country})": int(row.keyword_id)
        for row in frame.itertuples()
    }
    assert len(labels) == 2
    for keyword_id in labels.values():
        selected = frame[frame["keyword_id"] == keyword_id]
        assert len(selected) == 1


def test_every_column_the_keywords_view_renders_exists(conn: sqlite3.Connection) -> None:
    scored_keyword(conn)
    frame = dashboard._frame(repo.latest_scores(conn, sort="opportunity"))
    for column in ("keyword", "country", "tags", "opportunity_score",
                   "search_score", "competition_score", "captured_at"):
        assert column in frame.columns, column


def test_every_column_the_movers_view_renders_exists(conn: sqlite3.Connection) -> None:
    keyword_id = scored_keyword(conn, captured_at=days_ago(10), opportunity=20.0)
    repo.write_snapshot(
        conn,
        repo.SnapshotWrite(
            keyword_id=keyword_id, captured_at=days_ago(0),
            search_score=70.0, competition_score=40.0, opportunity_score=42.0,
        ),
    )
    frame = dashboard._frame(repo.score_movers(conn, days=7))
    for column in ("keyword", "country", "baseline_opportunity", "opportunity_score",
                   "opportunity_delta", "search_delta", "competition_delta"):
        assert column in frame.columns, column


def test_every_column_the_serp_table_renders_exists(conn: sqlite3.Connection) -> None:
    keyword_id = scored_keyword(conn)
    conn.execute(
        "INSERT INTO apps (track_id, country, track_name, seller_name, "
        "user_rating_count, average_user_rating, fetched_at) "
        "VALUES (1, 'us', 'An App', 'A Seller', 100, 4.5, ?)",
        (days_ago(0),),
    )
    repo.write_serp(conn, keyword_id, days_ago(0), [1])
    frame = dashboard._frame(repo.latest_serp(conn, keyword_id))
    for column in ("rank", "track_name", "seller_name", "user_rating_count",
                   "average_user_rating", "captured_at"):
        assert column in frame.columns, column


def test_component_columns_match_the_configured_weights() -> None:
    """The detail view zips these against stored snapshot columns."""
    from aso.config import COMPETITION_WEIGHTS
    from aso.repository import SnapshotWrite

    stored = SnapshotWrite.__dataclass_fields__
    for component in dashboard.COMPONENT_COLUMNS:
        assert component in COMPETITION_WEIGHTS
        assert component in stored, f"{component} is not a snapshot column"


# --- timestamp formatting --------------------------------------------------


def test_when_formats_a_stored_timestamp() -> None:
    assert dashboard.when("2026-08-01T12:30:00Z") == "2026-08-01 12:30 UTC"


def test_when_handles_never_and_garbage() -> None:
    assert dashboard.when(None) == "never"
    assert dashboard.when("") == "never"
    assert dashboard.when("not a date") == "not a date"


# --- the read-only promise -------------------------------------------------


def dashboard_source() -> str:
    return open(dashboard.__file__, encoding="utf-8").read()


def test_the_dashboard_builds_no_http_client_of_its_own() -> None:
    """Network access is allowed now, but only through `aso.lookup`.

    The lookup screen needs to reach Apple, so the old "no network at all" rule
    is gone. What replaces it is narrower: this module may not construct
    clients or a Fetcher itself. Keeping the one network path behind a named
    function in another module is what makes "I/O happens only on submit"
    reviewable — a `Fetcher(...)` inline in a render function would run on
    every rerun and nothing here could tell.
    """
    text = dashboard_source()
    for forbidden in ("ITunesClient", "HintsClient", "Fetcher(", "httpx", "asyncio"):
        assert forbidden not in text, f"dashboard constructs {forbidden} itself"


def test_the_dashboard_still_writes_no_measurements() -> None:
    """Keyword management writes; the data pipeline does not.

    Adding and deleting keywords is a user action. Writing snapshots, SERPs,
    demand observations or rescored values is the pipeline's job, and a
    dashboard that did it would produce history nobody could reproduce with
    `aso refresh`.
    """
    text = dashboard_source()
    for forbidden in ("write_snapshot", "write_serp",
                      "write_demand_observations", "update_snapshot_scores"):
        assert forbidden not in text, f"dashboard calls {forbidden}"


def test_every_lookup_call_is_guarded_by_a_submit() -> None:
    """The network call must never sit at render scope.

    Streamlit reruns the whole script on every widget interaction. A lookup at
    render scope would therefore fire on scrolls, sorts and filter changes —
    ~13 requests each against a 15/min budget, which trips Apple's 403
    threshold in under a minute and takes any running `aso refresh` with it.

    Asserted structurally: every call to `lookup_module.lookup(...)` must be
    nested inside an `if`, and the enclosing function must obtain a
    `form_submit_button`. This is a coarse check, but it fails loudly if
    someone later hoists the call to the top of a view.
    """
    tree = ast.parse(dashboard_source())

    def calls_lookup(node: ast.AST) -> bool:
        return any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "lookup"
            for inner in ast.walk(node)
        )

    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    callers = [fn for fn in functions if calls_lookup(fn)]
    assert callers, "no function calls lookup — did the lookup view get renamed?"

    for fn in callers:
        guarded = [n for n in ast.walk(fn) if isinstance(n, ast.If) and calls_lookup(n)]
        assert guarded, f"{fn.name} calls lookup outside any `if`"
        assert "form_submit_button" in ast.dump(fn), (
            f"{fn.name} calls lookup without a form submit to gate it"
        )


def test_deleting_a_keyword_removes_its_history(conn: sqlite3.Connection) -> None:
    keyword_id = scored_keyword(conn, "forex")
    conn.execute(
        "INSERT INTO apps (track_id, country, track_name, fetched_at) "
        "VALUES (1, 'us', 'An App', ?)",
        (days_ago(0),),
    )
    repo.write_serp(conn, keyword_id, days_ago(0), [1])

    removed = repo.delete_keyword(conn, keyword_id)

    assert removed == {"keywords": 1, "snapshots": 1, "serps": 1}
    assert repo.get_keyword(conn, "forex", "us") is None
    assert conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE keyword_id = ?", (keyword_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM serps WHERE keyword_id = ?", (keyword_id,)
    ).fetchone()[0] == 0


def test_deleting_a_keyword_keeps_its_demand_observations(
    conn: sqlite3.Connection,
) -> None:
    """Vendor measurements outlive the decision to track a term."""
    keyword_id = scored_keyword(conn, "forex")
    repo.write_demand_observations(
        conn,
        [repo.DemandWrite(source="appfigures", scale="ordinal_100",
                          keyword="forex", country="us", value=42.0)],
    )

    repo.delete_keyword(conn, keyword_id)

    assert conn.execute(
        "SELECT COUNT(*) FROM demand_observations WHERE keyword = 'forex'"
    ).fetchone()[0] == 1


def test_footprint_reports_what_a_delete_would_destroy(
    conn: sqlite3.Connection,
) -> None:
    keyword_id = scored_keyword(conn, "forex")
    assert repo.keyword_footprint(conn, keyword_id) == {"snapshots": 1, "serps": 0}


def test_pausing_a_keyword_keeps_its_history(conn: sqlite3.Connection) -> None:
    """The reversible option the Manage screen recommends by default."""
    keyword_id = scored_keyword(conn, "forex")
    repo.set_active(conn, keyword_id, False)

    assert repo.get_keyword(conn, "forex", "us")["active"] == 0
    assert len(repo.snapshot_history(conn, keyword_id)) == 1

    repo.set_active(conn, keyword_id, True)
    assert repo.get_keyword(conn, "forex", "us")["active"] == 1


# --- how the server is configured to listen --------------------------------


def streamlit_config() -> str:
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml"
    assert path.exists(), "no .streamlit/config.toml — Streamlit defaults would apply"
    return path.read_text(encoding="utf-8")


def test_the_dashboard_binds_loopback_only() -> None:
    """No auth by design, so it must not be reachable from the network.

    Streamlit binds 0.0.0.0 by default and prints an "External URL" on
    startup, which would publish keyword research and competitor tracking to
    anyone on the same network.
    """
    import tomllib

    config = tomllib.loads(streamlit_config())
    assert config["server"]["address"] == "localhost"


def test_usage_telemetry_is_off() -> None:
    import tomllib

    config = tomllib.loads(streamlit_config())
    assert config["browser"]["gatherUsageStats"] is False
