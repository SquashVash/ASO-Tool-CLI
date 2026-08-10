from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from aso import cli, db, repository as repo

from .conftest import FIXTURES
from .test_hints import HINTS_URL
from .test_http import URL as ITUNES_URL

SERP_BODY = (FIXTURES / "itunes_search_candlestick_us.json").read_text(encoding="utf-8")
HINTS_BODY = (FIXTURES / "hints_candlestick_us.plist").read_text(encoding="utf-8")

runner = CliRunner()


@pytest.fixture
def isolated_db(isolated_environment: Path) -> Path:
    """The temp database the autouse fixture in conftest already pointed at."""
    db.init_db(isolated_environment)
    return isolated_environment


def invoke(*args: str):
    return runner.invoke(cli.app, list(args))


# Every top-charts feed, on any storefront. These tests care that the index
# is built at all, not which genre a given app charts in.
CHARTS_URL_PATTERN = r"https://itunes\.apple\.com/[a-z]{2}/rss/.+/json"
CHARTS_BODY = (FIXTURES / "charts_finance_free_us.json").read_text(encoding="utf-8")


def mock_charts(status: int = 200) -> None:
    respx.get(url__regex=CHARTS_URL_PATTERN).mock(
        return_value=httpx.Response(status, text=CHARTS_BODY if status == 200 else "nope")
    )


def mock_apple() -> None:
    mock_charts()
    respx.get(ITUNES_URL).mock(return_value=httpx.Response(200, text=SERP_BODY))
    respx.get(HINTS_URL).mock(return_value=httpx.Response(200, text=HINTS_BODY))


# --- basics ----------------------------------------------------------------


def test_version() -> None:
    result = invoke("version")
    assert result.exit_code == 0
    assert "aso" in result.stdout


def test_init_is_idempotent() -> None:
    assert invoke("init").exit_code == 0
    second = invoke("init")
    assert second.exit_code == 0
    assert "up to date" in second.stdout


def test_no_args_shows_help() -> None:
    assert invoke().exit_code != 0 or "Usage" in invoke().stdout


# --- add -------------------------------------------------------------------


def test_add_tracks_a_keyword(isolated_db: Path) -> None:
    result = invoke("add", "candlestick patterns", "--country", "us", "--tag", "lcp")
    assert result.exit_code == 0
    with db.session(isolated_db) as conn:
        row = repo.get_keyword(conn, "candlestick patterns", "us")
    assert row is not None
    assert row["tags"] == "lcp"


def test_add_accepts_repeated_tags(isolated_db: Path) -> None:
    invoke("add", "forex", "-t", "lcp", "-t", "trading")
    with db.session(isolated_db) as conn:
        assert repo.get_keyword(conn, "forex", "us")["tags"] == "lcp,trading"


def test_readding_merges_rather_than_erroring(isolated_db: Path) -> None:
    invoke("add", "forex", "-t", "lcp")
    result = invoke("add", "forex", "-t", "extra")
    assert result.exit_code == 0
    assert "Already tracked" in result.stdout
    with db.session(isolated_db) as conn:
        assert repo.get_keyword(conn, "forex", "us")["tags"] == "extra,lcp"


def test_add_rejects_a_blank_keyword() -> None:
    assert invoke("add", "   ").exit_code == 1


# --- import ----------------------------------------------------------------


def test_import_reads_keyword_country_and_tags(tmp_path: Path, isolated_db: Path) -> None:
    csv_path = tmp_path / "keywords.csv"
    csv_path.write_text(
        "keyword,country,tags\n"
        "candlestick patterns,us,lcp;charts\n"
        "day trading,de,lcp\n",
        encoding="utf-8",
    )
    result = invoke("import", str(csv_path))
    assert result.exit_code == 0
    with db.session(isolated_db) as conn:
        assert len(repo.list_keywords(conn)) == 2
        assert repo.get_keyword(conn, "candlestick patterns", "us")["tags"] == "charts,lcp"
        assert repo.get_keyword(conn, "day trading", "de") is not None


def test_import_falls_back_to_the_default_country(tmp_path: Path, isolated_db: Path) -> None:
    csv_path = tmp_path / "k.csv"
    csv_path.write_text("keyword\nforex\n", encoding="utf-8")
    invoke("import", str(csv_path), "--country", "gb")
    with db.session(isolated_db) as conn:
        assert repo.get_keyword(conn, "forex", "gb") is not None


def test_import_is_repeatable(tmp_path: Path, isolated_db: Path) -> None:
    csv_path = tmp_path / "k.csv"
    csv_path.write_text("keyword\nforex\n", encoding="utf-8")
    invoke("import", str(csv_path))
    result = invoke("import", str(csv_path))
    assert "0 new, 1 already tracked" in result.stdout
    with db.session(isolated_db) as conn:
        assert len(repo.list_keywords(conn)) == 1


def test_import_without_a_keyword_column_fails_clearly(tmp_path: Path) -> None:
    csv_path = tmp_path / "wrong.csv"
    csv_path.write_text("term,volume\nforex,100\n", encoding="utf-8")
    result = invoke("import", str(csv_path))
    assert result.exit_code == 1


def test_import_skips_blank_rows_and_keeps_going(tmp_path: Path, isolated_db: Path) -> None:
    csv_path = tmp_path / "k.csv"
    csv_path.write_text("keyword\nforex\n\ngold\n", encoding="utf-8")
    result = invoke("import", str(csv_path))
    assert result.exit_code == 0
    with db.session(isolated_db) as conn:
        assert {r["keyword"] for r in repo.list_keywords(conn)} == {"forex", "gold"}


def test_import_handles_a_bom(tmp_path: Path, isolated_db: Path) -> None:
    """Excel writes UTF-8 with a BOM, which otherwise corrupts the first header."""
    csv_path = tmp_path / "excel.csv"
    csv_path.write_text("keyword\nforex\n", encoding="utf-8-sig")
    assert invoke("import", str(csv_path)).exit_code == 0
    with db.session(isolated_db) as conn:
        assert repo.get_keyword(conn, "forex", "us") is not None


def test_import_of_a_missing_file_fails() -> None:
    assert invoke("import", "nope.csv").exit_code != 0


# --- refresh ---------------------------------------------------------------


@respx.mock
def test_refresh_scores_tracked_keywords(isolated_db: Path) -> None:
    mock_apple()
    invoke("add", "candlestick patterns", "-t", "lcp")
    result = invoke("refresh", "--tag", "lcp")
    assert result.exit_code == 0
    assert "1 scored" in result.stdout

    with db.session(isolated_db) as conn:
        row = repo.require_keyword(conn, "candlestick patterns", "us")
        assert repo.latest_snapshot(conn, row["id"])["opportunity_score"] is not None


def test_refresh_with_no_matching_keywords_says_so() -> None:
    result = invoke("refresh", "--tag", "nothing")
    assert result.exit_code == 0
    assert "No keywords match" in result.stdout


def test_refresh_of_an_unknown_single_keyword_fails() -> None:
    result = invoke("refresh", "-k", "never added")
    assert result.exit_code == 1


@respx.mock
def test_refresh_reports_failures_without_crashing(isolated_db: Path) -> None:
    mock_charts(403)
    respx.get(ITUNES_URL).mock(return_value=httpx.Response(403))
    respx.get(HINTS_URL).mock(return_value=httpx.Response(403))
    invoke("add", "forex")
    result = invoke("refresh")
    assert result.exit_code == 0
    with db.session(isolated_db) as conn:
        row = repo.require_keyword(conn, "forex", "us")
        assert repo.latest_snapshot(conn, row["id"])["fetch_failed"] == 1


# --- rescore ---------------------------------------------------------------


def test_rescore_with_no_snapshots_says_so(isolated_db: Path) -> None:
    result = invoke("rescore")
    assert result.exit_code == 0
    assert "No snapshots" in result.stdout


@respx.mock
def test_rescore_reports_what_it_touched(isolated_db: Path) -> None:
    mock_apple()
    invoke("add", "candlestick patterns")
    invoke("refresh")
    result = invoke("rescore")
    assert result.exit_code == 0
    assert "Re-scored 1 snapshot(s)" in result.stdout


@respx.mock
def test_rescore_makes_no_network_requests(isolated_db: Path) -> None:
    """The whole point: retuning weights must not cost a single Apple call."""
    mock_apple()
    invoke("add", "candlestick patterns")
    invoke("refresh")
    before = respx.calls.call_count
    invoke("rescore")
    assert respx.calls.call_count == before


# --- list ------------------------------------------------------------------


def test_list_with_nothing_tracked_is_friendly() -> None:
    result = invoke("list")
    assert result.exit_code == 0
    assert "No keywords tracked" in result.stdout


def test_list_shows_unrefreshed_keywords(isolated_db: Path) -> None:
    invoke("add", "forex")
    result = invoke("list")
    assert result.exit_code == 0
    assert "forex" in result.stdout
    assert "never refreshed" in result.stdout


@respx.mock
def test_list_sorts_by_opportunity_by_default(isolated_db: Path) -> None:
    mock_apple()
    invoke("add", "candlestick patterns")
    invoke("refresh")
    result = invoke("list")
    assert result.exit_code == 0
    assert "candlestick patterns" in result.stdout


def test_list_rejects_an_unknown_sort() -> None:
    result = invoke("list", "--sort", "wibble")
    assert result.exit_code == 1


# --- show ------------------------------------------------------------------


def test_show_of_an_untracked_keyword_fails_clearly() -> None:
    result = invoke("show", "never added")
    assert result.exit_code == 1


def test_show_of_an_unrefreshed_keyword_explains(isolated_db: Path) -> None:
    invoke("add", "forex")
    result = invoke("show", "forex")
    assert result.exit_code == 0
    assert "Never refreshed" in result.stdout


@respx.mock
def test_show_renders_components_trend_and_serp(isolated_db: Path) -> None:
    mock_apple()
    invoke("add", "candlestick patterns")
    invoke("refresh")
    result = invoke("show", "candlestick patterns")
    assert result.exit_code == 0
    assert "opportunity" in result.stdout
    assert "rating_count" in result.stdout
    assert "NOT App Store rank" in result.stdout


# --- track -----------------------------------------------------------------


def test_track_with_no_data_is_friendly() -> None:
    result = invoke("track", "--track-id", "627114159")
    assert result.exit_code == 0
    assert "doesn't appear" in result.stdout


@respx.mock
def test_track_finds_an_app_in_the_stored_ranking(isolated_db: Path) -> None:
    mock_apple()
    invoke("add", "candlestick patterns")
    invoke("refresh")
    with db.session(isolated_db) as conn:
        row = repo.require_keyword(conn, "candlestick patterns", "us")
        track_id = repo.latest_serp(conn, row["id"])[0]["track_id"]

    result = invoke("track", "--track-id", str(track_id))
    assert result.exit_code == 0
    assert "candlestick patterns" in result.stdout
    assert "not App Store rank" in result.stdout


# --- export ----------------------------------------------------------------


@respx.mock
def test_export_csv_includes_every_component(tmp_path: Path, isolated_db: Path) -> None:
    mock_apple()
    invoke("add", "candlestick patterns")
    invoke("refresh")

    out = tmp_path / "out.csv"
    result = invoke("export", "--format", "csv", "--output", str(out))
    assert result.exit_code == 0
    header = out.read_text(encoding="utf-8").splitlines()[0]
    for column in ("keyword", "country", "opportunity_score", "comp_rating_count",
                   "search_prefix_depth", "search_hint_rank"):
        assert column in header


@respx.mock
def test_export_json_round_trips(tmp_path: Path, isolated_db: Path) -> None:
    mock_apple()
    invoke("add", "candlestick patterns")
    invoke("refresh")

    out = tmp_path / "out.json"
    assert invoke("export", "-f", "json", "-o", str(out)).exit_code == 0
    records = json.loads(out.read_text(encoding="utf-8"))
    assert records[0]["keyword"] == "candlestick patterns"
    assert records[0]["opportunity_score"] is not None


def test_export_to_stdout_when_no_output_given(isolated_db: Path) -> None:
    invoke("add", "forex")
    result = invoke("export", "--format", "csv")
    assert result.exit_code == 0
    assert "keyword" in result.stdout


def test_export_rejects_an_unknown_format() -> None:
    assert invoke("export", "--format", "xml").exit_code == 1


def test_export_creates_missing_parent_directories(tmp_path: Path, isolated_db: Path) -> None:
    invoke("add", "forex")
    out = tmp_path / "nested" / "dir" / "out.csv"
    assert invoke("export", "-o", str(out)).exit_code == 0
    assert out.exists()


def test_export_to_an_unwritable_path_fails_cleanly(tmp_path: Path, isolated_db: Path) -> None:
    """A bad path should print a message, not a traceback."""
    invoke("add", "forex")
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    result = invoke("export", "-o", str(blocker / "out.csv"))
    assert result.exit_code == 1


def test_export_of_an_empty_database_writes_a_header(tmp_path: Path) -> None:
    out = tmp_path / "empty.csv"
    assert invoke("export", "-o", str(out)).exit_code == 0
    assert "keyword" in out.read_text(encoding="utf-8")


# --- asa / calibrate -------------------------------------------------------


def test_asa_commands_without_credentials_explain_what_is_missing() -> None:
    """The common first-run state. It must read as setup advice, not a crash."""
    for args in (("asa", "whoami"), ("asa", "campaigns"), ("asa", "pull")):
        result = invoke(*args)
        assert result.exit_code == 1, args
        assert "ASO_ASA_CLIENT_ID" in result.output, args


def test_asa_pull_rejects_a_nonsense_window() -> None:
    assert invoke("asa", "pull", "--days", "0").exit_code == 1


def test_calibrate_with_no_measured_data_says_what_to_do(isolated_db: Path) -> None:
    result = invoke("calibrate")
    assert result.exit_code == 1
    assert "asa pull" in result.output


def test_calibrate_reports_the_fit(isolated_db: Path) -> None:
    """Enough joined samples to actually fit."""
    import random

    rng = random.Random(3)
    with db.session(isolated_db) as conn:
        for i in range(40):
            keyword = f"keyword number {i}"
            repo.add_keyword(conn, keyword, "us")
            row = repo.require_keyword(conn, keyword, "us")
            depth = rng.randint(1, 6)
            rank = rng.randint(1, 10)
            repo.write_snapshot(
                conn,
                repo.SnapshotWrite(
                    keyword_id=row["id"],
                    captured_at="2026-08-01T00:00:00Z",
                    search_prefix_depth=depth,
                    search_hint_rank=rank,
                ),
            )
            repo.write_demand_observations(
                conn,
                [
                    repo.DemandWrite(
                        source="asa", scale="count", keyword=keyword,
                        country="us", value=10_000 / (depth * rank),
                    )
                ],
            )

    result = invoke("calibrate")
    assert result.exit_code == 0
    assert "SEARCH_DEPTH_DECAY" in result.output
    assert "rank correlation" in result.output


def test_calibrate_json_is_machine_readable(isolated_db: Path) -> None:
    import random

    rng = random.Random(5)
    with db.session(isolated_db) as conn:
        for i in range(40):
            keyword = f"term {i}"
            repo.add_keyword(conn, keyword, "us")
            row = repo.require_keyword(conn, keyword, "us")
            repo.write_snapshot(
                conn,
                repo.SnapshotWrite(
                    keyword_id=row["id"], captured_at="2026-08-01T00:00:00Z",
                    search_prefix_depth=rng.randint(1, 6),
                    search_hint_rank=rng.randint(1, 10),
                ),
            )
            repo.write_demand_observations(
                conn,
                [
                    repo.DemandWrite(
                        source="asa", scale="count", keyword=keyword,
                        country="us", value=float(rng.randint(10, 50_000)),
                    )
                ],
            )

    result = invoke("calibrate", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "SEARCH_DEPTH_DECAY" in payload
    assert payload["n_samples"] == 40


def test_calibrate_never_writes_the_constants_itself(isolated_db: Path) -> None:
    """A scoring constant that rewrites itself is one nobody reviews."""
    before = Path("aso/config.py").read_text(encoding="utf-8")
    invoke("calibrate")
    assert Path("aso/config.py").read_text(encoding="utf-8") == before


def test_every_db_command_applies_pending_migrations(isolated_environment: Path) -> None:
    """A stale database must not produce a traceback.

    Found by hand: `calibrate` and `asa pull` queried a table added in a later
    migration without applying it first, so any database created before that
    migration crashed with `no such table`.
    """
    from aso import db as db_module

    # A database at migration 1 only — as an existing install would be.
    conn = db_module.connect(isolated_environment)
    with db_module.transaction(conn):
        for statement in db_module.split_statements(db_module.MIGRATIONS[0][2]):
            conn.execute(statement)
        conn.execute(db_module.SCHEMA_MIGRATIONS_DDL)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (1, 'x', 'y')"
        )
    conn.close()

    for args in (("list",), ("rescore",), ("calibrate",), ("export",)):
        result = invoke(*args)
        assert "no such table" not in result.output, args
        assert not isinstance(result.exception, Exception) or isinstance(
            result.exception, SystemExit
        ), f"{args} raised {result.exception!r}"


# --- import-demand ---------------------------------------------------------


def test_import_demand_stores_and_tracks(tmp_path: Path, isolated_db: Path) -> None:
    csv_path = tmp_path / "af.csv"
    csv_path.write_text(
        "Keyword,Popularity,Competitiveness,Total\n"
        "habit tracker,59,76,250\n"
        "streaks,50,85,250\n",
        encoding="utf-8",
    )
    result = invoke("import-demand", str(csv_path), "--country", "us", "--track")
    assert result.exit_code == 0
    assert "2 appfigures observation(s)" in result.output

    with db.session(isolated_db) as conn:
        assert [r["source"] for r in repo.demand_sources(conn)] == ["appfigures"]
        assert repo.get_keyword(conn, "habit tracker", "us") is not None


def test_import_demand_without_track_leaves_keywords_alone(
    tmp_path: Path, isolated_db: Path
) -> None:
    csv_path = tmp_path / "af.csv"
    csv_path.write_text("Keyword,Popularity\nforex,42\n", encoding="utf-8")
    assert invoke("import-demand", str(csv_path)).exit_code == 0
    with db.session(isolated_db) as conn:
        assert repo.list_keywords(conn) == []


def test_import_demand_rejects_the_wrong_file(tmp_path: Path, isolated_db: Path) -> None:
    csv_path = tmp_path / "wrong.csv"
    csv_path.write_text("term,volume\nforex,100\n", encoding="utf-8")
    result = invoke("import-demand", str(csv_path))
    assert result.exit_code == 1


def test_calibrate_names_the_source_it_used(tmp_path: Path, isolated_db: Path) -> None:
    import random

    rng = random.Random(9)
    lines = ["Keyword,Popularity"]
    with db.session(isolated_db) as conn:
        for i in range(30):
            keyword = f"keyword number {i}"
            depth, rank = rng.randint(1, 6), rng.randint(1, 10)
            lines.append(f"{keyword},{max(1, 100 - depth * 8 - rank * 3)}")
            repo.add_keyword(conn, keyword, "us")
            row = repo.require_keyword(conn, keyword, "us")
            repo.write_snapshot(
                conn,
                repo.SnapshotWrite(
                    keyword_id=row["id"], captured_at="2026-08-01T00:00:00Z",
                    search_prefix_depth=depth, search_hint_rank=rank,
                ),
            )

    csv_path = tmp_path / "af.csv"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert invoke("import-demand", str(csv_path)).exit_code == 0

    result = invoke("calibrate")
    assert result.exit_code == 0
    assert "appfigures" in result.output
    assert "SEARCH_DEPTH_DECAY" in result.output


def test_calibrate_explains_when_demand_exists_but_nothing_joins(
    tmp_path: Path, isolated_db: Path
) -> None:
    """The likely first-run failure: demand imported, but no keyword refreshed yet."""
    csv_path = tmp_path / "af.csv"
    csv_path.write_text(
        "Keyword,Popularity\n" + "".join(f"kw{i},{i + 1}\n" for i in range(30)),
        encoding="utf-8",
    )
    invoke("import-demand", str(csv_path), "--track")
    result = invoke("calibrate")
    assert result.exit_code == 1
    assert "30 appfigures observation(s) are stored" in result.output
    assert "aso refresh" in result.output


def test_calibrate_rejects_an_unknown_source(tmp_path: Path, isolated_db: Path) -> None:
    csv_path = tmp_path / "af.csv"
    csv_path.write_text("Keyword,Popularity\nforex,42\n", encoding="utf-8")
    invoke("import-demand", str(csv_path))
    result = invoke("calibrate", "--source", "sensortower")
    assert result.exit_code == 1
    assert "appfigures" in result.output


def test_import_demand_stratify_tracks_a_spread_not_everything(
    tmp_path: Path, isolated_db: Path
) -> None:
    """Each tracked keyword costs a ladder walk, so spend the budget on spread."""
    lines = ["Keyword,Popularity"]
    lines += [f"floor{i},5" for i in range(40)]
    lines += [f"real{v},{v}" for v in (10, 20, 30, 40, 50, 60)]
    csv_path = tmp_path / "af.csv"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = invoke("import-demand", str(csv_path), "--stratify", "6")
    assert result.exit_code == 0
    assert "46 appfigures observation(s)" in result.output, "all demand is stored"

    with db.session(isolated_db) as conn:
        tracked = repo.list_keywords(conn)
        assert len(tracked) == 6, "but only the sampled keywords are tracked"
        names = {r["keyword"] for r in tracked}
    assert sum(1 for n in names if n.startswith("floor")) <= 1, (
        "a 40/6 floor-heavy input must not track mostly floor keywords"
    )


# --- non-ASCII keywords ----------------------------------------------------


@respx.mock
def test_a_japanese_keyword_does_not_kill_the_run(isolated_db: Path) -> None:
    """A real crash: cp1252 stdout raised UnicodeEncodeError mid-refresh.

    The pipeline guarantees fetch failures are recorded rather than fatal, but
    this happened in the display layer, so a single unprintable keyword took
    down an entire 56-keyword run.
    """
    mock_apple()
    assert invoke("add", "ハビットトラッカー").exit_code == 0
    assert invoke("add", "habit tracker®").exit_code == 0

    result = invoke("refresh")
    assert result.exit_code == 0
    assert result.exception is None or isinstance(result.exception, SystemExit)

    with db.session(isolated_db) as conn:
        assert len(repo.list_keywords(conn)) == 2


def test_listing_and_exporting_survive_non_ascii(tmp_path: Path, isolated_db: Path) -> None:
    invoke("add", "ハビットトラッカー")
    assert invoke("list").exit_code == 0

    out = tmp_path / "out.csv"
    assert invoke("export", "-o", str(out)).exit_code == 0
    assert "ハビットトラッカー" in out.read_text(encoding="utf-8")


def test_show_renders_a_non_ascii_keyword(isolated_db: Path) -> None:
    invoke("add", "ハビットトラッカー")
    assert invoke("show", "ハビットトラッカー").exit_code == 0


# --- check -----------------------------------------------------------------


@respx.mock
def test_check_scores_without_tracking(isolated_db: Path) -> None:
    """The whole point: answer "is this worth tracking?" without committing."""
    mock_apple()
    result = invoke("check", "candlestick patterns")
    assert result.exit_code == 0
    assert "opportunity" in result.output

    with db.session(isolated_db) as conn:
        assert repo.list_keywords(conn) == [], "no keyword row"
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM serps").fetchone()[0] == 0


@respx.mock
def test_check_shows_components_and_the_serp(isolated_db: Path) -> None:
    mock_apple()
    result = invoke("check", "candlestick patterns")
    assert "comp_rating_count" in result.output
    assert "search_prefix_depth" in result.output
    assert "NOT App Store rank" in result.output


@respx.mock
def test_check_agrees_with_refresh_on_the_same_keyword(isolated_db: Path) -> None:
    """Both paths must run identical scoring — only persistence differs."""
    mock_apple()
    checked = json.loads(invoke("check", "candlestick patterns", "--json").stdout)

    invoke("add", "candlestick patterns")
    invoke("refresh")
    with db.session(isolated_db) as conn:
        row = repo.require_keyword(conn, "candlestick patterns", "us")
        stored = repo.latest_snapshot(conn, row["id"])

    for field in ("search_score", "competition_score", "opportunity_score",
                  "comp_rating_count", "search_prefix_depth", "search_hint_rank"):
        assert checked[field] == pytest.approx(stored[field]), field


@respx.mock
def test_check_json_marks_the_result_as_untracked(isolated_db: Path) -> None:
    mock_apple()
    payload = json.loads(invoke("check", "forex", "--json").stdout)
    assert payload["tracked"] is False
    assert payload["keyword"] == "forex"
    assert payload["country"] == "us"


@respx.mock
def test_check_reports_a_failure_without_crashing(isolated_db: Path) -> None:
    mock_charts(403)
    respx.get(ITUNES_URL).mock(return_value=httpx.Response(403))
    respx.get(HINTS_URL).mock(return_value=httpx.Response(403))
    result = invoke("check", "forex")
    assert result.exit_code == 0
    assert "failed" in result.output or "serp" in result.output


def test_check_rejects_a_blank_keyword() -> None:
    assert invoke("check", "   ").exit_code == 1


@respx.mock
def test_check_says_it_did_not_track(isolated_db: Path) -> None:
    """A tool that scores silently without storing must say so."""
    mock_apple()
    result = invoke("check", "forex")
    assert "Not tracked" in result.output
    assert "aso add" in result.output
