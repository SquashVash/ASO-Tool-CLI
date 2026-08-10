"""Measured observations, fitted bridges, and the samples the fits train on.

Replaces the half of `repository.py` that served calibration. Three files under
`data/`, all of them small and all of them checked in:

* `demand_observations.json` — what a source measured a keyword's demand to be
* `competition_observations.json` — what a vendor rated its difficulty
* `bridges.json` — the fitted maps from this tool's scale onto theirs

WHY THE CORPUS IS A SEPARATE FILE
---------------------------------
A fit needs (observation, components) pairs, and the components used to come
from the latest snapshot of a tracked keyword. Clearing the keyword list would
therefore have emptied the training set and silently changed every fitted
constant — the weights would still be in `config.py`, but nobody could re-derive
them.

So the component side is frozen once into `calibration_corpus.json`: 231
keywords as they scored on 2026-08-09, independent of what you happen to be
tracking now. `samples()` reads the corpus and the live keyword list together,
with the live list winning on `(keyword, country)`. Today's fits reproduce
exactly against an empty list, and anything you track and refresh from here on
joins the training set on its own.

WHAT REMAINS TRUE OF FREEZING
-----------------------------
The corpus ages. It describes what Apple's autocomplete and charts said on one
day, and a component recomputed a year from now against the same keyword may
not match. `aso refresh` is what keeps a keyword current; the corpus is what
keeps the fit reproducible when you are not tracking anything.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEMAND_SOURCE_PRIORITY, settings
from .files import read_rows, utcnow, write_rows
from .scoring.bridge import Bridge
from .store import Store, normalize_keyword

DEMAND_FILE = "demand_observations.json"
COMPETITION_FILE = "competition_observations.json"
BRIDGES_FILE = "bridges.json"
CORPUS_FILE = "calibration_corpus.json"

# The component fields every competition fit reads. Kept complete on purpose:
# the fit's job is to decide which components deserve weight, so handing it
# only the ones that currently have some would let today's weights decide
# tomorrow's. `comp_stars` earning nothing has to be a finding, not an input.
COMPONENTS = (
    "comp_rating_count",
    "comp_exact_match",
    "comp_stars",
    "comp_recency",
    "comp_publisher",
    "comp_breadth",
    "comp_incumbent",
    "comp_app_power",
)


def _path(name: str) -> Path:
    return settings.data_dir / name


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemandWrite:
    """One source's measurement of a keyword's demand in a storefront."""

    source: str
    scale: str
    keyword: str
    country: str
    value: float
    censored: bool = False


@dataclass(frozen=True)
class CompetitionWrite:
    """One vendor's difficulty rating for a keyword in a storefront.

    No `censored` flag, unlike `DemandWrite`. Demand sources withhold values
    below a reporting threshold, which is information worth keeping; the
    difficulty indexes seen so far either score a keyword or omit the row
    entirely, and an omitted row is absent rather than bounded.
    """

    source: str
    scale: str
    keyword: str
    country: str
    value: float


def demand_observations() -> list[dict[str, Any]]:
    return read_rows(_path(DEMAND_FILE), "observations")


def competition_observations() -> list[dict[str, Any]]:
    return read_rows(_path(COMPETITION_FILE), "observations")


def write_demand_observations(rows: Sequence[DemandWrite]) -> int:
    """Upsert demand observations. Re-importing a source replaces its values."""
    existing = {
        (r["source"], r["keyword"], r["country"]): r for r in demand_observations()
    }
    now = utcnow()
    for row in rows:
        key = (row.source, normalize_keyword(row.keyword), row.country.lower())
        existing[key] = {
            "source": row.source,
            "scale": row.scale,
            "keyword": key[1],
            "country": key[2],
            "value": 0.0 if row.censored else float(row.value),
            "censored": 1 if row.censored else 0,
            "captured_at": now,
        }
    merged = sorted(
        existing.values(), key=lambda r: (r["source"], r["country"], r["keyword"])
    )
    write_rows(_path(DEMAND_FILE), "observations", merged)
    return len(rows)


def write_competition_observations(rows: Sequence[CompetitionWrite]) -> int:
    """Upsert competition observations. Re-importing a source replaces its values."""
    existing = {
        (r["source"], r["keyword"], r["country"]): r
        for r in competition_observations()
    }
    now = utcnow()
    for row in rows:
        key = (row.source, normalize_keyword(row.keyword), row.country.lower())
        existing[key] = {
            "source": row.source,
            "scale": row.scale,
            "keyword": key[1],
            "country": key[2],
            "value": float(row.value),
            "captured_at": now,
        }
    merged = sorted(
        existing.values(), key=lambda r: (r["source"], r["country"], r["keyword"])
    )
    write_rows(_path(COMPETITION_FILE), "observations", merged)
    return len(rows)


def demand_map(source: str = "apple") -> dict[tuple[str, str], tuple[float | None, bool]]:
    """(keyword, country) -> (value, censored) for one demand source.

    Loaded whole rather than looked up per keyword: a rescore walks every
    record, and this file is small enough to hold entirely.
    """
    return {
        (row["keyword"], row["country"]): (
            None if row["censored"] else row["value"],
            bool(row["censored"]),
        )
        for row in demand_observations()
        if row["source"] == source
    }


def demand_sources() -> list[dict[str, Any]]:
    """Available sources with their scale and row count, richest first."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in demand_observations():
        key = (row["source"], row["scale"])
        entry = grouped.setdefault(
            key, {"source": row["source"], "scale": row["scale"], "rows": 0, "captured_at": ""}
        )
        entry["rows"] += 1
        entry["captured_at"] = max(entry["captured_at"], row.get("captured_at") or "")
    return sorted(grouped.values(), key=lambda r: -r["rows"])


def preferred_demand_source(
    priority: Sequence[str] = DEMAND_SOURCE_PRIORITY,
) -> str | None:
    """The highest-priority demand source that actually has uncensored rows.

    Calibration fits ONE source. Which one is not a user's problem to remember,
    and it is not a question to answer by row count either: AppFigures will
    usually have the most rows and is the weakest of the three, being a model
    of the number `apple` reports directly.

    Censored rows do not count. A source asked about 400 keywords that scored
    none of them has 400 rows and no ordering, and picking it over a smaller
    source with real values would be choosing the emptier evidence.
    """
    counts: dict[str, int] = {}
    for row in demand_observations():
        if not row["censored"] and row["value"] > 0:
            counts[row["source"]] = counts.get(row["source"], 0) + 1
    for source in priority:
        if counts.get(source):
            return source
    return None


# ---------------------------------------------------------------------------
# bridges
# ---------------------------------------------------------------------------


def bridges() -> list[dict[str, Any]]:
    return read_rows(_path(BRIDGES_FILE), "bridges")


def to_bridge(row: dict[str, Any] | None) -> "Bridge | None":
    """Rebuild a `Bridge` from a stored record, or None if there isn't one.

    Knots are stored as a nested list rather than the JSON string the database
    held, so this re-encodes for `Bridge.from_json`. Keeping the fitted object's
    own parser as the single way to build one means the stored shape is
    validated the same way whether it came from a fit or from the file.
    """
    if row is None:
        return None
    return Bridge.from_json(
        json.dumps(row["knots"]),
        n_overlap=row["n_overlap"],
        rmse=row["rmse"] or 0.0,
    )


def latest_bridge(
    *, country: str, source: str = "apple", metric: str = "demand"
) -> dict[str, Any] | None:
    """The newest bridge of one metric for this storefront, or None.

    `metric` separates the demand bridge from the competition one. They are the
    same kind of object fitted for the same reason, and without it a vendor
    that rates both would have each overwrite the other's "newest".
    """
    matches = [
        b
        for b in bridges()
        if b["country"] == country.lower()
        and b["source"] == source
        and b.get("metric", "demand") == metric
    ]
    if not matches:
        return None
    return max(matches, key=lambda b: b.get("fitted_at") or "")


def write_bridge(
    *,
    country: str,
    source: str,
    knots_json: str,
    n_overlap: int,
    rmse: float | None,
    metric: str = "demand",
) -> None:
    """Store a fitted bridge, replacing any previous fit of the same metric.

    The database kept these append-only so the scale a given snapshot was
    scored under stayed auditable. With the snapshot history gone there is no
    longer anything to audit against — every record is rescored under the
    current bridge — so keeping superseded fits would only grow the file with
    rows nothing reads. The fit that is in force is the one that is stored.
    """
    kept = [
        b
        for b in bridges()
        if not (
            b["country"] == country.lower()
            and b["source"] == source
            and b.get("metric", "demand") == metric
        )
    ]
    kept.append(
        {
            "country": country.lower(),
            "source": source,
            "metric": metric,
            "knots": json.loads(knots_json),
            "n_overlap": n_overlap,
            "rmse": rmse,
            "fitted_at": utcnow(),
        }
    )
    kept.sort(key=lambda b: (b["metric"], b["country"], b["source"]))
    write_rows(_path(BRIDGES_FILE), "bridges", kept)


# ---------------------------------------------------------------------------
# training samples
# ---------------------------------------------------------------------------


def corpus() -> list[dict[str, Any]]:
    """The frozen component rows, keyed by (keyword, country)."""
    return read_rows(_path(CORPUS_FILE), "keywords")


def scored_keywords(store: Store | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    """Frozen corpus overlaid with anything currently tracked and scored.

    The live list wins, so refreshing a keyword that is also in the corpus
    trains the fit on the fresh measurement rather than the frozen one.
    """
    merged = {
        (r["keyword"], r["country"]): r for r in corpus() if r.get("active", 1)
    }
    live = store if store is not None else Store.load()
    for record in live.scored():
        if record.get("active"):
            merged[(record["keyword"], record["country"])] = record
    return merged


def demand_samples(source: str, store: Store | None = None) -> list[dict[str, Any]]:
    """Measured demand joined to the latest ladder observation per keyword.

    The entire input to `scoring.search.calibrate()`. Joined on (keyword,
    country) so a German measurement can never calibrate a US keyword.

    A NULL `prefix_depth` is KEPT, and that is the point of the filter's shape.
    Requiring depth and rank would silently drop every keyword autocomplete
    never surfaced — the majority on the reference sample, and precisely the
    ones the extensions and rating-mass components exist to score. What must be
    excluded is a row that observed nothing at all.
    """
    known = scored_keywords(store)
    samples = []
    for obs in demand_observations():
        if obs["source"] != source or obs["value"] <= 0:
            continue
        row = known.get((obs["keyword"], obs["country"]))
        if row is None:
            continue
        if not (
            row.get("search_hint_extensions") is not None
            or row.get("comp_rating_count") is not None
            or (
                row.get("search_prefix_depth") is not None
                and row.get("search_hint_rank") is not None
            )
        ):
            continue
        samples.append(
            {
                "keyword": obs["keyword"],
                "country": obs["country"],
                "prefix_depth": row.get("search_prefix_depth"),
                "hint_rank": row.get("search_hint_rank"),
                "extensions": row.get("search_hint_extensions"),
                "rating_mass": row.get("comp_rating_count"),
                "value": obs["value"],
                "scale": obs["scale"],
            }
        )
    samples.sort(key=lambda r: -r["value"])
    return samples


def competition_samples(source: str, store: Store | None = None) -> list[dict[str, Any]]:
    """Vendor difficulty joined to the latest components per keyword.

    The mirror of `demand_samples` on the competition side, and the entire
    input to `scoring.competition.calibrate()`.
    """
    known = scored_keywords(store)
    samples = []
    for obs in competition_observations():
        row = known.get((obs["keyword"], obs["country"]))
        if obs["source"] != source or row is None:
            continue
        samples.append(
            {
                "keyword": obs["keyword"],
                "country": obs["country"],
                **{name: row.get(name) for name in COMPONENTS},
                "value": obs["value"],
                "scale": obs["scale"],
            }
        )
    samples.sort(key=lambda r: -r["value"])
    return samples


def bridge_pairs(*, country: str, source: str = "apple", store: Store | None = None) -> list[dict[str, Any]]:
    """(proxy_score, measured) for keywords carrying both.

    The input to `scoring.bridge.fit`. Restricted to ONE country: popularity is
    a per-storefront quantity, so a map fitted across markets would average the
    US and Japanese relationships into one that describes neither.

    Reads `search_score_proxy` rather than `search_score`, because after a
    blend the latter may already BE the measured value for exactly the keywords
    in this overlap — fitting a map from Apple's numbers onto Apple's numbers
    would return the identity and look like a perfect fit.

    Censored rows are excluded. Their value is a bound, not a level, and a
    bound cannot anchor a scale.
    """
    known = scored_keywords(store)
    pairs = []
    for obs in demand_observations():
        if obs["source"] != source or obs["censored"] or obs["value"] <= 0:
            continue
        if obs["country"] != country.lower():
            continue
        row = known.get((obs["keyword"], obs["country"]))
        if row is None or row.get("search_score_proxy") is None:
            continue
        pairs.append(
            {
                "keyword": obs["keyword"],
                "proxy_score": row["search_score_proxy"],
                "measured": obs["value"],
            }
        )
    return pairs
