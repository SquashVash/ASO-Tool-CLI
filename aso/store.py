"""The tracked keyword list, in one JSON file.

Replaces `repository.py`'s keyword and snapshot queries. The shape is the same
as it always was minus one dimension: a keyword carries its *latest* scores,
not a history of them.

WHAT WENT AWAY WITH THE HISTORY
-------------------------------
`snapshots` recorded one row per keyword per refresh, and `serps` recorded the
ranking behind it. Between them they answered "is this keyword trending up?"
and "where does my app sit for it?", which is what `aso track`, `aso show`'s
trend column and the dashboard's charts were for. Those are gone, deliberately:
the history cost 46,000 SERP rows and a growing database, and nothing in the
scoring path ever read it.

What replaced it is `refresh` overwriting the score fields in place. Every
component is still stored beside the finals, so `rescore` can still recompute
any score from its own record without going back to Apple — that property was
never about history, and it survives.

WHY A CLASS AND NOT MODULE FUNCTIONS
------------------------------------
The old repository functions each took a `sqlite3.Connection` and SQLite did
the read-modify-write. A JSON file has to be loaded whole, mutated, and written
back, so something has to own the loaded rows between those steps. `session()`
makes the boundary explicit and writes once at the end rather than rewriting
the file on every mutation.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import settings
from .files import read_rows, utcnow, write_rows

FILENAME = "keywords.json"
KEY = "keywords"

# Sort keys accepted by `latest_scores`, mapped to record fields.
SORT_FIELDS = {
    "opportunity": "opportunity_score",
    "search": "search_score",
    "competition": "competition_score",
    "keyword": "keyword",
    "captured": "captured_at",
}

# The score fields a refresh writes and a rescore may rewrite. Listed so that
# `write_scores` can clear the ones a failed fetch did not produce, rather than
# leaving a stale number from the previous run standing next to a fresh error.
SCORE_FIELDS = (
    "captured_at",
    "search_score",
    "search_score_proxy",
    "search_source",
    "competition_score",
    "competition_score_raw",
    "opportunity_score",
    "comp_rating_count",
    "comp_exact_match",
    "comp_stars",
    "comp_recency",
    "comp_publisher",
    "comp_breadth",
    "comp_incumbent",
    "comp_app_power",
    "search_prefix_depth",
    "search_hint_rank",
    "search_hint_extensions",
    "fetch_failed",
    "fetch_error",
)


class UnknownKeyword(LookupError):
    def __init__(self, keyword: str, country: str) -> None:
        self.keyword = keyword
        self.country = country
        super().__init__(f"No tracked keyword {keyword!r} in storefront {country!r}")


# ---------------------------------------------------------------------------
# normalization
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
    are one keyword. Storing them case-preserved would let two records claim
    the same keyword and silently double the refresh cost.
    """
    return " ".join(keyword.split()).lower()


def _has_tag(record: dict[str, Any], tag: str) -> bool:
    return tag.strip().lower() in split_tags(record.get("tags"))


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


@dataclass
class Store:
    """The keyword list, loaded. Mutate, then `save()`."""

    path: Path
    records: list[dict[str, Any]] = field(default_factory=list)
    dirty: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> "Store":
        target = path or (settings.data_dir / FILENAME)
        return cls(path=target, records=read_rows(target, KEY))

    def save(self) -> None:
        write_rows(self.path, KEY, self.records)
        self.dirty = False

    # -- keywords -----------------------------------------------------------

    def add_keyword(
        self,
        keyword: str,
        country: str,
        tags: Iterable[str] | str | None = None,
    ) -> tuple[int, bool]:
        """Add a keyword, or merge tags into the existing one.

        Returns `(keyword_id, created)`. Re-adding an existing keyword with a
        new tag adds the tag rather than erroring or replacing the tag set —
        importing an overlapping CSV should be safe to repeat.
        """
        keyword = normalize_keyword(keyword)
        country = country.strip().lower()
        if not keyword:
            raise ValueError("keyword cannot be empty")
        if not country:
            raise ValueError("country cannot be empty")

        existing = self.get_keyword(keyword, country)
        if existing is not None:
            merged = normalize_tags(
                split_tags(existing.get("tags")) + split_tags(normalize_tags(tags))
            )
            if merged != (existing.get("tags") or ""):
                existing["tags"] = merged
                self.dirty = True
            return int(existing["id"]), False

        # Ids are assigned here rather than by position, because the API
        # addresses keywords by id and a delete must not renumber the rest.
        next_id = max((int(r["id"]) for r in self.records), default=0) + 1
        record: dict[str, Any] = {
            "id": next_id,
            "keyword": keyword,
            "country": country,
            "tags": normalize_tags(tags),
            "active": 1,
            "created_at": utcnow(),
        }
        record.update({name: None for name in SCORE_FIELDS})
        record["fetch_failed"] = 0
        self.records.append(record)
        self.dirty = True
        return next_id, True

    def get_keyword(self, keyword: str, country: str) -> dict[str, Any] | None:
        keyword = normalize_keyword(keyword)
        country = country.strip().lower()
        for record in self.records:
            if record["keyword"] == keyword and record["country"] == country:
                return record
        return None

    def get_keyword_by_id(self, keyword_id: int) -> dict[str, Any] | None:
        """Resolve an id, for callers that address keywords by id.

        The API does, because keywords contain spaces, unicode and sometimes
        '/', which no amount of URL encoding makes pleasant in a path segment.
        """
        for record in self.records:
            if int(record["id"]) == int(keyword_id):
                return record
        return None

    def require_keyword(self, keyword: str, country: str) -> dict[str, Any]:
        record = self.get_keyword(keyword, country)
        if record is None:
            raise UnknownKeyword(keyword, country)
        return record

    def list_keywords(
        self,
        *,
        tag: str | None = None,
        country: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        rows = [
            r
            for r in self.records
            if (not active_only or r.get("active"))
            and (not country or r["country"] == country.strip().lower())
            and (not tag or _has_tag(r, tag))
        ]
        return sorted(rows, key=lambda r: (r["keyword"], r["country"]))

    def set_active(self, keyword_id: int, active: bool) -> None:
        record = self.get_keyword_by_id(keyword_id)
        if record is not None:
            record["active"] = 1 if active else 0
            self.dirty = True

    def set_tags(self, keyword_id: int, tags: Iterable[str] | str | None) -> None:
        """Replace a keyword's tag set.

        `add_keyword` merges, because re-importing an overlapping CSV must be
        safe. Replacing is the other half: without it there is no way to
        remove a tag.
        """
        record = self.get_keyword_by_id(keyword_id)
        if record is not None:
            record["tags"] = normalize_tags(tags)
            self.dirty = True

    def delete_keyword(self, keyword_id: int) -> dict[str, int]:
        """Permanently remove a keyword and the scores attached to it.

        Prefer `set_active(..., False)`: deactivating stops refreshes and drops
        the keyword out of calibration while keeping its scores, and it is
        reversible. This is not.

        Observations are deliberately untouched. They key on the keyword text
        rather than its id, and they are measurements from a vendor export that
        remain true whether or not you track the term. They stay available to a
        later `aso calibrate` if you add the keyword back, and are inert
        meanwhile because the calibration join goes through this list.
        """
        before = len(self.records)
        self.records = [r for r in self.records if int(r["id"]) != int(keyword_id)]
        removed = before - len(self.records)
        self.dirty = self.dirty or bool(removed)
        return {"keywords": removed}

    def all_tags(self) -> list[str]:
        seen: set[str] = set()
        for record in self.records:
            seen.update(split_tags(record.get("tags")))
        return sorted(seen)

    def countries(self) -> list[str]:
        return sorted({r["country"] for r in self.records})

    # -- scores -------------------------------------------------------------

    def write_scores(self, keyword_id: int, **scores: Any) -> None:
        """Replace one keyword's scores with what this run measured.

        Every field in `SCORE_FIELDS` is written, including the ones the caller
        did not supply. A refresh that fails must not leave last week's
        opportunity score sitting beside this run's error message — the record
        would then read as fresh and be wrong.
        """
        record = self.get_keyword_by_id(keyword_id)
        if record is None:
            raise UnknownKeyword(str(keyword_id), "")
        unknown = set(scores) - set(SCORE_FIELDS)
        if unknown:
            raise ValueError(f"Not score fields: {', '.join(sorted(unknown))}")
        for name in SCORE_FIELDS:
            record[name] = scores.get(name)
        record["fetch_failed"] = 1 if scores.get("fetch_failed") else 0
        record["captured_at"] = scores.get("captured_at") or utcnow()
        self.dirty = True

    def update_scores(self, keyword_id: int, **finals: Any) -> None:
        """Overwrite only the finals, leaving measured components alone.

        What `rescore` uses. The components and the raw ladder observations are
        the *inputs* to a re-score and must never be touched by it;
        `search_score_proxy` and `comp_incumbent` are outputs despite their
        names, because both are computed from stored inputs and move whenever
        the mapping or the formula does.
        """
        record = self.get_keyword_by_id(keyword_id)
        if record is None:
            raise UnknownKeyword(str(keyword_id), "")
        allowed = {
            "search_score",
            "search_score_proxy",
            "search_source",
            "competition_score",
            "competition_score_raw",
            "opportunity_score",
            "comp_incumbent",
        }
        unknown = set(finals) - allowed
        if unknown:
            raise ValueError(f"Not final scores: {', '.join(sorted(unknown))}")
        record.update(finals)
        self.dirty = True

    def scored(self) -> list[dict[str, Any]]:
        """Records carrying a usable measurement, for the fits to train on.

        `fetch_failed` is the filter that matters: "Apple returned no match"
        and "we never got an answer from Apple" look identical in the depth
        column and mean opposite things, and only the first is a training
        point.
        """
        return [
            r
            for r in self.records
            if r.get("captured_at") and not r.get("fetch_failed")
        ]

    def latest_scores(
        self,
        *,
        tag: str | None = None,
        country: str | None = None,
        sort: str = "opportunity",
        limit: int | None = None,
        active_only: bool = True,
        include_unscored: bool = True,
    ) -> list[dict[str, Any]]:
        """Every keyword with its scores, sorted.

        `include_unscored` keeps keywords that have never been refreshed, so a
        freshly imported set doesn't silently vanish from `aso list`.
        """
        if sort not in SORT_FIELDS:
            raise ValueError(
                f"Unknown sort {sort!r}. Choose from: {', '.join(SORT_FIELDS)}"
            )
        rows = self.list_keywords(tag=tag, country=country, active_only=active_only)
        if not include_unscored:
            rows = [r for r in rows if r.get("captured_at")]

        # `ORDER BY (col IS NULL) ASC, col DESC, keyword ASC`, in three stable
        # passes. Partitioning on the missing value rather than sorting by it
        # keeps NULLs last under either direction, and sorting the keyword
        # tiebreak first lets the stable sort preserve it. Both `opportunity`
        # and `captured` go through here, so the comparison has to work for a
        # float and an ISO timestamp alike — which rules out negating the value.
        name = SORT_FIELDS[sort]
        rows = sorted(rows, key=lambda r: r["keyword"])
        present = [r for r in rows if r.get(name) is not None]
        missing = [r for r in rows if r.get(name) is None]
        present.sort(key=lambda r: r[name], reverse=sort != "keyword")
        rows = present + missing
        return rows[:limit] if limit is not None else rows


@contextmanager
def session(path: Path | None = None) -> Iterator[Store]:
    """Load the keyword list, hand it over, and write it back if it changed.

    Writes only on a clean exit and only when something was mutated, so a
    read-only command leaves the file's mtime alone and a command that raises
    leaves the previous list in place.
    """
    store = Store.load(path)
    yield store
    if store.dirty:
        store.save()
