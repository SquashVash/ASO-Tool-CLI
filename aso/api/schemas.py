"""Response models.

Every model carrying a score also carries `captured_at`. A caller that cannot
distinguish a fresh score from a three-week-old one will treat stale data as
current, and the whole point of this API is that the caller is a machine.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..repository import split_tags


class ComponentWeight(BaseModel):
    name: str
    value: float | None
    weight: float


class KeywordScore(BaseModel):
    keyword_id: int
    keyword: str
    country: str
    tags: list[str]
    active: bool
    captured_at: str | None
    search_score: float | None
    competition_score: float | None
    opportunity_score: float | None
    fetch_failed: bool
    fetch_error: str | None

    @classmethod
    def from_row(cls, row) -> "KeywordScore":
        return cls(
            keyword_id=row["keyword_id"],
            keyword=row["keyword"],
            country=row["country"],
            tags=split_tags(row["tags"]),
            active=bool(row["active"]),
            captured_at=row["captured_at"],
            search_score=row["search_score"],
            competition_score=row["competition_score"],
            opportunity_score=row["opportunity_score"],
            fetch_failed=bool(row["fetch_failed"]),
            fetch_error=row["fetch_error"],
        )


class SnapshotRow(BaseModel):
    captured_at: str
    search_score: float | None
    competition_score: float | None
    competition_score_raw: float | None
    opportunity_score: float | None
    search_prefix_depth: int | None
    search_hint_rank: int | None
    fetch_failed: bool
    fetch_error: str | None

    @classmethod
    def from_row(cls, row) -> "SnapshotRow":
        return cls(
            captured_at=row["captured_at"],
            search_score=row["search_score"],
            competition_score=row["competition_score"],
            competition_score_raw=row["competition_score_raw"],
            opportunity_score=row["opportunity_score"],
            search_prefix_depth=row["search_prefix_depth"],
            search_hint_rank=row["search_hint_rank"],
            fetch_failed=bool(row["fetch_failed"]),
            fetch_error=row["fetch_error"],
        )


class KeywordDetail(BaseModel):
    keyword_id: int
    keyword: str
    country: str
    tags: list[str]
    active: bool
    latest: SnapshotRow | None
    components: list[ComponentWeight]


class SerpRow(BaseModel):
    rank: int
    track_id: int
    captured_at: str
    track_name: str | None
    seller_name: str | None
    user_rating_count: int | None
    average_user_rating: float | None
    current_version_release_date: str | None

    @classmethod
    def from_row(cls, row) -> "SerpRow":
        return cls(**{key: row[key] for key in cls.model_fields})


class MoverRow(BaseModel):
    """A null delta means *not measured then*, never *did not move*."""

    keyword_id: int
    keyword: str
    country: str
    tags: list[str]
    captured_at: str
    baseline_at: str | None
    opportunity_score: float | None
    search_score: float | None
    competition_score: float | None
    opportunity_delta: float | None
    search_delta: float | None
    competition_delta: float | None

    @classmethod
    def from_row(cls, row) -> "MoverRow":
        data = {key: row[key] for key in cls.model_fields if key != "tags"}
        data["tags"] = split_tags(row["tags"])
        return cls(**data)
