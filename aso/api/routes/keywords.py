"""Reading and editing the tracked keyword list. No network."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ...config import COMPETITION_WEIGHTS
from ...store import Store, normalize_keyword, split_tags
from ..deps import get_store
from ..schemas import (
    AddKeywordRequest,
    AddKeywordResponse,
    ComponentWeight,
    DeleteResponse,
    KeywordDetail,
    KeywordScore,
    PatchKeywordRequest,
    ScoreRow,
)

router = APIRouter(tags=["keywords"])


def _require_keyword_row(store: Store, keyword_id: int) -> dict:
    """Resolve an id to its keyword record, or 404."""
    row = store.get_keyword_by_id(keyword_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No keyword with id {keyword_id}")
    return row


@router.get("/keywords", response_model=list[KeywordScore])
def list_keywords(
    store: Store = Depends(get_store),
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
    limit: int | None = Query(None, ge=1),
    include_inactive: bool = False,
    include_unscored: bool = True,
) -> list[KeywordScore]:
    # `limit` truncates before the `keyword` filter runs. Passing it straight
    # through would make id-resolution depend on where the match happens to
    # fall in the sorted, limited window — a caller doing `?keyword=foo&limit=1`
    # could get an empty result even though "foo" is tracked, just because
    # something else sorted ahead of it. So when `keyword` is set, take the
    # full candidate set and apply `limit` after filtering instead.
    store_limit = None if keyword is not None else limit
    try:
        rows = store.latest_scores(
            tag=tag,
            country=country,
            sort=sort,
            limit=store_limit,
            active_only=not include_inactive,
            include_unscored=include_unscored,
        )
    except ValueError as exc:  # unknown sort key
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if keyword is not None:
        wanted = normalize_keyword(keyword)
        rows = [row for row in rows if row["keyword"] == wanted]
        if limit is not None:
            rows = rows[:limit]
    return [KeywordScore.from_row(row) for row in rows]


@router.get("/keywords/{keyword_id}", response_model=KeywordDetail)
def keyword_detail(
    keyword_id: int, store: Store = Depends(get_store)
) -> KeywordDetail:
    row = _require_keyword_row(store, keyword_id)
    scored = bool(row.get("captured_at"))
    components = [
        ComponentWeight(name=name, value=row.get(name), weight=weight)
        for name, weight in COMPETITION_WEIGHTS.items()
    ]
    return KeywordDetail(
        keyword_id=row["id"],
        keyword=row["keyword"],
        country=row["country"],
        tags=split_tags(row["tags"]),
        active=bool(row["active"]),
        latest=ScoreRow.from_row(row) if scored else None,
        components=components,
    )


@router.post("/keywords", response_model=AddKeywordResponse)
def add_keyword(
    body: AddKeywordRequest,
    response: Response,
    store: Store = Depends(get_store),
) -> AddKeywordResponse:
    """Add a tracked keyword, merging tags into an existing one.

    Merging rather than replacing matches `Store.add_keyword`: re-posting an
    overlapping set must be safe to repeat.
    """
    try:
        keyword_id, created = store.add_keyword(body.keyword, body.country, body.tags)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response.status_code = 201 if created else 200
    return AddKeywordResponse(keyword_id=keyword_id, created=created)


@router.patch("/keywords/{keyword_id}", response_model=KeywordDetail)
def patch_keyword(
    keyword_id: int,
    body: PatchKeywordRequest,
    store: Store = Depends(get_store),
) -> KeywordDetail:
    _require_keyword_row(store, keyword_id)
    if body.active is not None:
        store.set_active(keyword_id, body.active)
    if body.tags is not None:
        store.set_tags(keyword_id, body.tags)
    return keyword_detail(keyword_id, store)


@router.delete("/keywords/{keyword_id}", response_model=DeleteResponse)
def delete_keyword(
    keyword_id: int, store: Store = Depends(get_store)
) -> DeleteResponse:
    """Permanent. Prefer PATCH {"active": false}, which is reversible."""
    _require_keyword_row(store, keyword_id)
    return DeleteResponse(**store.delete_keyword(keyword_id))
