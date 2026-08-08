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


def mock_apple() -> None:
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
    respx.get(ITUNES_URL).mock(return_value=httpx.Response(403))
    respx.get(HINTS_URL).mock(return_value=httpx.Response(403))
    invoke("add", "forex")
    result = invoke("refresh")
    assert result.exit_code == 0
    with db.session(isolated_db) as conn:
        row = repo.require_keyword(conn, "forex", "us")
        assert repo.latest_snapshot(conn, row["id"])["fetch_failed"] == 1


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
