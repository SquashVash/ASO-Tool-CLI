"""Readers for demand data exported from tools that already measure it.

The autocomplete ladder infers demand. These files contain someone else's
measurement of it, and `scoring/search.py::calibrate()` fits the inference
against the measurement.

Currently one format: AppFigures' keyword exports. Adding another means one
more `Format` entry, not a new code path.

A note on what "measured" means here
------------------------------------
AppFigures' Popularity is itself derived from Apple's own keyword popularity
indicator — an ordinal 0-100 demand ranking, not a raw impression count. So
calibrating against it fits our proxy to a *better-sourced* proxy, one rung
closer to Apple than autocomplete, rather than to ground truth. That is a real
improvement and it is not the same as measuring impressions. The distinction is
carried through the schema as `scale = 'ordinal_100'` so the fit can treat it
correctly, and it is stated plainly in the README rather than glossed as
"calibrated".
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .repository import CompetitionWrite, DemandWrite
from .scoring.search import SCALE_COUNT, SCALE_ORDINAL_100


class ImportError_(ValueError):
    """A file we can't read as the claimed format."""


@dataclass(frozen=True)
class Format:
    """How to read one vendor's export."""

    name: str
    scale: str
    keyword_column: str
    value_column: str
    # Extra columns worth naming in the error message when the file is wrong.
    hint: str
    # The vendor's difficulty rating, when the export carries one. None means
    # this format measures demand only, and `read_competition_csv` will refuse
    # it rather than inventing a column.
    competition_column: str | None = None
    competition_scale: str = SCALE_ORDINAL_100


FORMATS: dict[str, Format] = {
    "appfigures": Format(
        name="appfigures",
        scale=SCALE_ORDINAL_100,
        keyword_column="keyword",
        value_column="popularity",
        hint="Keyword,Popularity,Competitiveness,Total",
        # Sat unread in this file for the whole life of the project while the
        # competition weights were being argued over rather than fitted.
        competition_column="competitiveness",
    ),
    # Generic escape hatch for anything already expressed as impression counts.
    "impressions": Format(
        name="impressions",
        scale=SCALE_COUNT,
        keyword_column="keyword",
        value_column="impressions",
        hint="keyword,impressions",
    ),
}


@dataclass
class ImportResult:
    rows: list[DemandWrite]
    skipped: list[str]

    @property
    def count(self) -> int:
        return len(self.rows)


def stratified_sample(rows: Sequence[DemandWrite], size: int) -> list[DemandWrite]:
    """Pick `size` rows spread evenly across the demand range.

    Calibration needs *spread*, not volume. A vendor export is dominated by
    long-tail terms sitting at the floor value — the first real run had 21 of
    26 keywords at AppFigures' floor of 5, which carries almost no ordering to
    fit. Taking the top N instead would be just as bad in the other direction:
    a fit that only ever saw popular keywords cannot rank unpopular ones.

    Sampling evenly over *rank position* does not work: it preserves the input
    distribution, so 90% floor in gives 90% floor out. What calibration needs
    is distinct demand *levels*, so this groups by value and round-robins
    across the groups — taking one keyword from each level before taking a
    second from any. That maximizes distinct target values for a fixed budget.

    The budget is what makes this worth doing: each keyword picked costs a full
    prefix-ladder walk against Apple, roughly a minute at the rate limit, so
    spending it on a new demand level rather than another tie matters a lot.
    """
    if size >= len(rows):
        return list(rows)
    if size <= 0:
        return []

    by_value: dict[float, list[DemandWrite]] = {}
    for row in rows:
        by_value.setdefault(row.value, []).append(row)

    # Highest demand first within the round-robin, so a budget too small to
    # cover every level still spans the informative end of the range.
    levels = [by_value[value] for value in sorted(by_value, reverse=True)]

    picked: list[DemandWrite] = []
    depth = 0
    while len(picked) < size:
        added = False
        for level in levels:
            if depth < len(level):
                picked.append(level[depth])
                added = True
                if len(picked) == size:
                    return picked
        if not added:  # pragma: no cover - size >= len(rows) is handled above
            break
        depth += 1
    return picked


def read_demand_csv(path: Path, *, source: str, country: str) -> ImportResult:
    """Read a vendor CSV into `DemandWrite` rows.

    `country` is a parameter rather than a column because these exports don't
    carry one — AppFigures puts the storefront in the *filename*
    (`related_keywords_75_hard-ios-handheld-us-2026_08_08.csv`), which is far
    too fragile to parse. Getting this wrong would silently calibrate a US
    keyword against German demand, so it is explicit and required.

    Bad rows are skipped and reported, never guessed at: a keyword with an
    unparseable value is not a keyword with zero demand.
    """
    fmt = FORMATS.get(source)
    if fmt is None:
        raise ImportError_(
            f"Unknown source {source!r}. Known: {', '.join(sorted(FORMATS))}."
        )

    try:
        # utf-8-sig: these exports open in Excel, which means a BOM.
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ImportError_(f"Cannot read {path}: {exc}") from exc

    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise ImportError_(f"{path} is empty.")

    columns = {(name or "").strip().lower(): name for name in reader.fieldnames}
    for needed in (fmt.keyword_column, fmt.value_column):
        if needed not in columns:
            raise ImportError_(
                f"{path} has no {needed!r} column, so it isn't a {source} export.\n"
                f"Expected something like: {fmt.hint}\n"
                f"Found: {', '.join(reader.fieldnames)}"
            )

    rows: list[DemandWrite] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for line_number, raw in enumerate(reader, start=2):
        keyword = (raw.get(columns[fmt.keyword_column]) or "").strip()
        if not keyword:
            skipped.append(f"line {line_number}: blank keyword")
            continue

        value_text = (raw.get(columns[fmt.value_column]) or "").strip()
        try:
            value = float(value_text)
        except ValueError:
            skipped.append(f"line {line_number}: {keyword!r} has non-numeric {fmt.value_column} {value_text!r}")
            continue

        if value <= 0:
            # Zero demand carries no gradient and, for ASA-style counts, means
            # "not measured" rather than "not searched".
            skipped.append(f"line {line_number}: {keyword!r} has no demand ({value:g})")
            continue

        key = keyword.casefold()
        if key in seen:
            skipped.append(f"line {line_number}: duplicate {keyword!r}")
            continue
        seen.add(key)

        rows.append(
            DemandWrite(
                source=fmt.name,
                scale=fmt.scale,
                keyword=keyword,
                country=country.lower(),
                value=value,
            )
        )

    return ImportResult(rows=rows, skipped=skipped)


@dataclass
class CompetitionImportResult:
    rows: list[CompetitionWrite]
    skipped: list[str]

    @property
    def count(self) -> int:
        return len(self.rows)


def read_competition_csv(
    path: Path, *, source: str, country: str
) -> CompetitionImportResult:
    """Read a vendor's difficulty column out of the same CSV `read_demand_csv` reads.

    Two passes over one file rather than one pass returning both, because the
    two columns disagree about which rows are usable: a keyword can carry a
    difficulty rating and no popularity, or the reverse, and a combined reader
    would have to drop any row missing either. That would silently shrink both
    training sets to their intersection.

    Unlike demand, a value of 0 is **kept**. Zero demand means "nobody searches
    this", which carries no gradient and is usually a reporting floor rather
    than a measurement; zero difficulty means "nothing is defending this term",
    which is a real and useful observation — it is the easy end of exactly the
    range the fit needs to see.
    """
    fmt = FORMATS.get(source)
    if fmt is None:
        raise ImportError_(
            f"Unknown source {source!r}. Known: {', '.join(sorted(FORMATS))}."
        )
    if fmt.competition_column is None:
        raise ImportError_(
            f"{source} exports carry no difficulty column, only demand. "
            "Use `aso import` for this file."
        )

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ImportError_(f"Cannot read {path}: {exc}") from exc

    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise ImportError_(f"{path} is empty.")

    columns = {(name or "").strip().lower(): name for name in reader.fieldnames}
    for needed in (fmt.keyword_column, fmt.competition_column):
        if needed not in columns:
            raise ImportError_(
                f"{path} has no {needed!r} column, so it isn't a {source} export.\n"
                f"Expected something like: {fmt.hint}\n"
                f"Found: {', '.join(reader.fieldnames)}"
            )

    rows: list[CompetitionWrite] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for line_number, raw in enumerate(reader, start=2):
        keyword = (raw.get(columns[fmt.keyword_column]) or "").strip()
        if not keyword:
            skipped.append(f"line {line_number}: blank keyword")
            continue

        value_text = (raw.get(columns[fmt.competition_column]) or "").strip()
        try:
            value = float(value_text)
        except ValueError:
            skipped.append(
                f"line {line_number}: {keyword!r} has non-numeric "
                f"{fmt.competition_column} {value_text!r}"
            )
            continue

        key = keyword.casefold()
        if key in seen:
            skipped.append(f"line {line_number}: duplicate {keyword!r}")
            continue
        seen.add(key)

        rows.append(
            CompetitionWrite(
                source=fmt.name,
                scale=fmt.competition_scale,
                keyword=keyword,
                country=country.lower(),
                value=value,
            )
        )

    return CompetitionImportResult(rows=rows, skipped=skipped)
