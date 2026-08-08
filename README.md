# aso

Self-hosted ASO keyword research for the iOS App Store. Scores keywords on
search volume and competition, stores a weekly snapshot of every score so
trends are visible, and ranks by opportunity.

Single-user, local, no auth, no server. Optimized for being read and modified.

> **Build status:** step 1 of 7 (scaffold, config, DB schema, migrations).
> The clients, scoring, pipeline, CLI commands and dashboard land in
> subsequent commits. Sections below marked _(pending)_ describe the design
> those commits implement.

---

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # create .venv and install everything
cp .env.example .env    # optional; defaults work without it
uv run aso init         # create aso.db and apply migrations
uv run pytest
```

Without `uv`:

```bash
python -m venv .venv
.venv\Scripts\activate        # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
aso init
```

Configuration is read from `.env` (see `.env.example`) with defaults in
`aso/config.py`. Nothing is required to get started.

---

## The big caveat, up front

**The iTunes Search API is not the App Store search index.**

`https://itunes.apple.com/search` is a separate, older content-search endpoint.
Its result ordering correlates with App Store ranking but is not the same
ranking, is not personalized, does not reflect Apple's current relevance model,
and does not account for Search Ads placements. Everything this tool derives
from it — the whole competition score, the stored SERPs — should be read as
*"here is the competitive field around this term"*, never as *"here is where
apps actually rank"*.

Similarly, the search score is a **proxy** derived from autocomplete behaviour,
not measured search volume. It stays a proxy until it is calibrated against
real Apple Search Ads impression data (`scoring/search.py::calibrate()`,
phase 2). Treat the numbers as ordinal — useful for ranking keywords against
each other, not as absolute demand estimates.

---

## Scoring methodology

Three numbers per keyword per snapshot, all 0–100.

### Competition (higher = harder) _(pending)_

Fetch the SERP for the keyword, take the top 10, compute six normalized
components, combine as a weighted mean. Weights live in
`config.COMPETITION_WEIGHTS` and can be changed at any time — see
_Re-scoring history_ below.

| Component | Weight | Definition |
|---|---|---|
| `comp_rating_count` | 0.35 | `min(log10(median(top10 rating counts) + 1) / 6, 1) × 100` |
| `comp_exact_match` | 0.25 | fraction of the top 10 with the full keyword in title or subtitle, × 100 |
| `comp_stars` | 0.10 | `median(top10 average rating) / 5 × 100` |
| `comp_recency` | 0.10 | median days since last update, mapped 0 days → 100, 365+ days → 0 |
| `comp_publisher` | 0.10 | fraction of the top 10 from sellers appearing 2+ times in the result set, × 100 |
| `comp_breadth` | 0.10 | `min(log10(total_result_count + 1) / 2.3, 1) × 100` |

`subtitle` and `averageUserRating` are frequently absent from the API response;
missing values are excluded from medians rather than treated as zero.

### Search volume (higher = more volume) _(pending)_

A prefix ladder. For keyword `k`, truncate from the right to build prefixes,
query autocomplete for each from shortest to longest, and record:

- **`prefix_depth`** — the shortest prefix at which `k` still appears in the
  suggestion list
- **`hint_rank`** — its position in that list

The reasoning: Apple's autocomplete surfaces high-demand completions early.
A term suggested from two characters in, at rank 1, is one many people type.
A term that only appears once you've typed the whole thing is one nobody
searches. The score is a weighted combination of how short that prefix was and
how high the rank was, with constants in `config.py`
(`SEARCH_DEPTH_WEIGHT`, `SEARCH_RANK_WEIGHT`, `SEARCH_RANK_DECAY`).

Capped at `SEARCH_MAX_PREFIX_QUERIES` (12) requests per keyword; longer
keywords have their ladder sampled. The ladder short-circuits: once a prefix
returns no match, nothing shorter is queried.

### Opportunity

```
opportunity = search_score × (100 - competition_score) / 100
```

The default sort in the CLI and dashboard.

### Re-scoring history

Every component that feeds a score is stored on the snapshot row next to the
final number. No score in the database is a bare figure you can't take apart.
That means retuning `COMPETITION_WEIGHTS`, or recalibrating the search mapping
against ASA data, re-scores the entire history without re-fetching anything
from Apple.

---

## Data sources

| Source | Used for | Status |
|---|---|---|
| `itunes.apple.com/search` | competitor set, SERP, app metadata | public, documented |
| `search.itunes.apple.com/.../MZSearchHints.woa` | autocomplete, search proxy | public |
| Apple Search Ads API v5 | ground-truth impressions, calibration | phase 2, stubbed |

No undocumented endpoints beyond those two public ones.

### Rate limiting _(pending)_

The iTunes endpoints start returning 403 above roughly 20 requests/minute per
IP. A shared async token bucket holds all iTunes and hints traffic to 15/min
with an `asyncio.Semaphore(3)` on top. 403/429 retry with exponential backoff
and jitter, giving up after 4 attempts and recording a failed-fetch marker on
the snapshot rather than aborting the run.

Caching is in SQLite, not memory, so a run is resumable: SERPs 3 days, app
metadata 7 days, autocomplete 3 days. A 500-keyword refresh takes roughly 30
minutes unattended and is safe to Ctrl-C and restart.

---

## CLI _(pending)_

```
aso init                                # create/upgrade the database
aso add "candlestick patterns" --country us --tag lcp
aso import keywords.csv
aso refresh --tag lcp --country us      # run the pipeline, write a snapshot
aso list --sort opportunity --limit 30
aso show "candlestick patterns"         # detail + trend + current top 10
aso track --track-id 627114159          # your app's rank across all keywords
aso export --format csv
```

## Dashboard _(pending)_

```bash
uv run streamlit run dashboard.py
```

Three views: a filterable keyword table, a per-keyword detail page with score
trends and the current top 10, and a movers view for week-over-week opportunity
changes.

---

## Data model

Four tables, in `aso/db.py`. `country` is a real column everywhere anything
varies by storefront — nothing defaults to `us` at the schema level.

- **`keywords`** — what's tracked. Unique on `(keyword, country)`.
- **`snapshots`** — one row per keyword per refresh: final scores, every
  component, the raw search observations, and a failed-fetch marker.
- **`apps`** — cached app metadata, keyed `(track_id, country)`.
- **`serps`** — ranked results per capture. Answers "who moved into the top 10
  last month" and backs `aso track`.

Migrations are an append-only list in `aso/db.py`; `aso init` applies anything
pending. Timestamps are ISO-8601 UTC strings.
