"""Reading what is already stored. No network, no writes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ... import repository
from ...config import COMPETITION_WEIGHTS
from ...db import transaction
from ...repository import split_tags
from ..deps import get_conn
from ..schemas import (
    AddKeywordRequest,
    AddKeywordResponse,
    ComponentWeight,
    DeleteResponse,
    KeywordDetail,
    KeywordScore,
    PatchKeywordRequest,
    SerpRow,
    SnapshotRow,
)

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
    # `limit` truncates in SQL before the `keyword` filter ever runs in Python.
    # Passing it straight through would make id-resolution depend on where the
    # match happens to fall in the sorted, limited window — a caller doing
    # `?keyword=foo&limit=1` could get an empty result even though "foo" is
    # tracked, just because something else sorted ahead of it. So when
    # `keyword` is set, fetch the full candidate set and apply `limit` after
    # filtering instead.
    sql_limit = None if keyword is not None else limit
    try:
        rows = repository.latest_scores(
            conn,
            tag=tag,
            country=country,
            sort=sort,
            limit=sql_limit,
            active_only=not include_inactive,
            include_unscored=include_unscored,
        )
    except ValueError as exc:  # unknown sort column
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if keyword is not None:
        wanted = repository.normalize_keyword(keyword)
        rows = [row for row in rows if row["keyword"] == wanted]
        if limit is not None:
            rows = rows[:limit]
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


@router.post("/keywords", response_model=AddKeywordResponse)
def add_keyword(
    body: AddKeywordRequest,
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
) -> AddKeywordResponse:
    """Add a tracked keyword, merging tags into an existing one.

    Merging rather than replacing matches `repository.add_keyword`: re-posting
    an overlapping set must be safe to repeat.
    """
    try:
        with transaction(conn):
            keyword_id, created = repository.add_keyword(
                conn, body.keyword, body.country, body.tags
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response.status_code = 201 if created else 200
    return AddKeywordResponse(keyword_id=keyword_id, created=created)


@router.patch("/keywords/{keyword_id}", response_model=KeywordDetail)
def patch_keyword(
    keyword_id: int,
    body: PatchKeywordRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> KeywordDetail:
    _require_keyword_row(conn, keyword_id)
    with transaction(conn):
        if body.active is not None:
            repository.set_active(conn, keyword_id, body.active)
        if body.tags is not None:
            repository.set_tags(conn, keyword_id, body.tags)
    return keyword_detail(keyword_id, conn)


@router.delete("/keywords/{keyword_id}", response_model=DeleteResponse)
def delete_keyword(
    keyword_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> DeleteResponse:
    """Permanent. Prefer PATCH {"active": false}, which is reversible."""
    _require_keyword_row(conn, keyword_id)
    with transaction(conn):
        counts = repository.delete_keyword(conn, keyword_id)
    return DeleteResponse(**counts)
