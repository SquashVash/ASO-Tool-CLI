"""Reading what is already stored. No network, no writes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from ... import repository
from ...config import COMPETITION_WEIGHTS
from ...repository import split_tags
from ..deps import get_conn
from ..schemas import ComponentWeight, KeywordDetail, KeywordScore, SerpRow, SnapshotRow

router = APIRouter(tags=["keywords"])


def _require_keyword_row(conn: sqlite3.Connection, keyword_id: int) -> sqlite3.Row:
    """Resolve an id to its keyword row, or 404."""
    row = repository.get_keyword_by_id(conn, keyword_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No keyword with id {keyword_id}")
    return row


@router.get("/keywords", response_model=list[KeywordScore])
def list_keywords(
    conn: sqlite3.Connection = Depends(get_conn),
    country: str | None = None,
    tag: str | None = None,
    keyword: str | None = Query(
        None,
        description=(
            "Exact match. Keywords are addressed by id in paths because they "
            "contain spaces, unicode, and sometimes '/'; this is how a caller "
            "holding only the string resolves one."
        ),
    ),
    sort: str = "opportunity",
    limit: int | None = None,
    include_inactive: bool = False,
    include_unscored: bool = True,
) -> list[KeywordScore]:
    try:
        rows = repository.latest_scores(
            conn,
            tag=tag,
            country=country,
            sort=sort,
            limit=limit,
            active_only=not include_inactive,
            include_unscored=include_unscored,
        )
    except ValueError as exc:  # unknown sort column
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if keyword is not None:
        wanted = repository.normalize_keyword(keyword)
        rows = [row for row in rows if row["keyword"] == wanted]
    return [KeywordScore.from_row(row) for row in rows]


@router.get("/keywords/{keyword_id}", response_model=KeywordDetail)
def keyword_detail(
    keyword_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> KeywordDetail:
    row = _require_keyword_row(conn, keyword_id)
    latest = repository.latest_snapshot(conn, keyword_id)
    components = [
        ComponentWeight(
            name=name,
            value=latest[name] if latest is not None else None,
            weight=weight,
        )
        for name, weight in COMPETITION_WEIGHTS.items()
    ]
    return KeywordDetail(
        keyword_id=row["id"],
        keyword=row["keyword"],
        country=row["country"],
        tags=split_tags(row["tags"]),
        active=bool(row["active"]),
        latest=SnapshotRow.from_row(latest) if latest is not None else None,
        components=components,
    )


@router.get("/keywords/{keyword_id}/history", response_model=list[SnapshotRow])
def keyword_history(
    keyword_id: int,
    limit: int | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[SnapshotRow]:
    _require_keyword_row(conn, keyword_id)
    rows = repository.snapshot_history(conn, keyword_id, limit=limit)
    return [SnapshotRow.from_row(row) for row in rows]


@router.get("/keywords/{keyword_id}/serp", response_model=list[SerpRow])
def keyword_serp(
    keyword_id: int,
    limit: int = 10,
    conn: sqlite3.Connection = Depends(get_conn),
) -> list[SerpRow]:
    _require_keyword_row(conn, keyword_id)
    return [SerpRow.from_row(row) for row in repository.latest_serp(conn, keyword_id, limit=limit)]
