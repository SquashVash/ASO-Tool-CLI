"""Response models.

Every model carrying a score also carries `captured_at`. A caller that cannot
distinguish a fresh score from a three-week-old one will treat stale data as
current, and the whole point of this API is that the caller is a machine.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..store import split_tags


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
            keyword_id=row["id"],
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


class ScoreRow(BaseModel):
    """A keyword's current scores, as last measured."""

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
    def from_row(cls, row) -> "ScoreRow":
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
    latest: ScoreRow | None
    components: list[ComponentWeight]


class AddKeywordRequest(BaseModel):
    keyword: str
    country: str
    tags: list[str] = []


class AddKeywordResponse(BaseModel):
    keyword_id: int
    created: bool


class PatchKeywordRequest(BaseModel):
    """Both fields optional; omitted means unchanged.

    `tags` REPLACES the tag set, unlike POST /keywords which merges. A caller
    that wants to remove a tag has no other way to say so.
    """

    active: bool | None = None
    tags: list[str] | None = None


class DeleteResponse(BaseModel):
    keywords: int


class LookupRequest(BaseModel):
    keyword: str
    country: str
    force: bool = False


class LookupResponse(BaseModel):
    keyword: str
    country: str
    search_score: float | None
    search_score_proxy: float | None
    search_source: str | None
    competition_score: float | None
    competition_score_raw: float | None
    opportunity_score: float | None
    components: dict[str, float | None]
    tracked: bool
    # Where this score falls among tracked keywords, 0-100. None when nothing
    # is tracked or scoring failed: an unscored keyword has no rank, and 0
    # would read as "the worst one".
    percentile: float | None
    compared_against: int
    # 0 means every response came from the HTTP cache.
    requests_made: int
    failed: bool
    error: str | None

    @classmethod
    def from_result(cls, result) -> "LookupResponse":
        outcome = result.scored.outcome
        return cls(
            keyword=outcome.keyword,
            country=outcome.country,
            search_score=outcome.search_score,
            search_score_proxy=outcome.search_score_proxy,
            search_source=outcome.search_source,
            competition_score=outcome.competition_score,
            competition_score_raw=outcome.competition_score_raw,
            opportunity_score=outcome.opportunity_score,
            components=result.scored.components,
            tracked=result.tracked,
            percentile=result.percentile,
            compared_against=result.compared_against,
            requests_made=result.requests_made,
            failed=outcome.failed,
            error=outcome.error,
        )


class RefreshRequest(BaseModel):
    tag: str | None = None
    country: str | None = None
    # `ge=1`: 0 would silently discard every matched keyword and then read
    # back as "no keywords match that filter", which is false — they
    # matched, the limit dropped them. Negative slices from the end of the
    # list, which is worse: a caller asking for "the first N" would get a
    # keyword they didn't ask for and no error to notice by.
    limit: int | None = Field(None, ge=1)
    force: bool = False
    include_inactive: bool = False


class JobResponse(BaseModel):
    id: str
    kind: str
    status: str
    started_at: str
    finished_at: str | None
    params: dict
    done: int
    total: int | None
    current: str | None
    result: dict | None
    error: str | None

    @classmethod
    def from_job(cls, job) -> "JobResponse":
        return cls(**{key: getattr(job, key) for key in cls.model_fields})


class ASAPullRequest(BaseModel):
    # `ge=1`: the handler computes `start = today - days`, so a negative value
    # makes `start > end` and sends Apple a window that runs backwards. 0 asks
    # for an empty window, which is never what a caller means.
    days: int | None = Field(None, ge=1)  # defaults to ASA_DEFAULT_LOOKBACK_DAYS


class PopularityPullRequest(BaseModel):
    country: str
    tag: str | None = None
    # `ge=1`, same as `RefreshRequest.limit`: 0 slices every matched keyword
    # away and then reports "No keywords match that filter", which is false.
    # Negative slices from the end, silently returning keywords not asked for.
    limit: int | None = Field(None, ge=1)


class RescoreResponse(BaseModel):
    total: int
    changed: int
    largest_move: float
    largest_move_keyword: str | None


class CandidateRow(BaseModel):
    term: str
    # The SHORTEST prefix that surfaced this term, and its rank in that list.
    # Apple orders by demand within one list; across two different prefixes
    # rank is NOT comparable, which is why results sort on prefix length first.
    prefix: str
    rank: int
    surfaced_by: int
    tracked: bool

    @classmethod
    def from_candidate(cls, candidate) -> "CandidateRow":
        return cls(**{key: getattr(candidate, key) for key in cls.model_fields})


class SuggestRequest(BaseModel):
    keyword: str
    country: str
    force: bool = False
    # Discovery answers "what am I missing", so tracked terms are noise by
    # default. The cost: a tracked keyword that has DROPPED OUT of Apple's
    # suggestions is then indistinguishable from one that never appeared.
    include_tracked: bool = False


class SuggestResponse(BaseModel):
    keyword: str
    country: str
    candidates: list[CandidateRow]
    prefixes_probed: list[str]
    requests_made: int
    # A failure part-way down the ladder keeps whatever was collected.
    failed: bool
    error: str | None

    @classmethod
    def from_result(cls, result) -> "SuggestResponse":
        return cls(
            keyword=result.keyword,
            country=result.country,
            candidates=[CandidateRow.from_candidate(c) for c in result.candidates],
            prefixes_probed=result.prefixes_probed,
            requests_made=result.requests_made,
            failed=result.failed,
            error=result.error,
        )
