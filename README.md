# aso

Self-hosted ASO keyword research for the iOS App Store. Scores keywords on
search volume and competition, stores a weekly snapshot of every score so
trends are visible, and ranks by opportunity.

Single-user, local, no auth, no server. Optimized for being read and modified.

> **Build status:** step 4 of 7 (scoring complete; pipeline, CLI, dashboard
> and the ASA stub remain).
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

Two specific things the endpoint does *not* give you, both confirmed against a
live capture (`tests/fixtures/itunes_search_candlestick_us.json`):

- **No subtitle.** The Search API returns no App Store subtitle field, so
  `comp_exact_match` matches on title only. It therefore *understates*
  competition for terms that incumbents target in their subtitle. `AppRecord`
  parses `subtitle` defensively in case that changes.
- **`resultCount` is a floor, not a total.** It counts what was returned, so it
  saturates at `limit` (50). `comp_breadth` separates thin niches from crowded
  ones but cannot tell 50 results from 5,000.

Similarly, the search score is a **proxy** derived from autocomplete behaviour,
not measured search volume. It stays a proxy until it is calibrated against
real Apple Search Ads impression data (`scoring/search.py::calibrate()`,
phase 2). Treat the numbers as ordinal — useful for ranking keywords against
each other, not as absolute demand estimates.

---

## Scoring methodology

Three numbers per keyword per snapshot, all 0–100.

### Competition (higher = harder)

Fetch the SERP for the keyword, take the top 10, compute six normalized
components, combine as a weighted mean. Weights live in
`config.COMPETITION_WEIGHTS` and can be changed at any time — see
_Re-scoring history_ below.

| Component | Weight | Definition |
|---|---|---|
| `comp_rating_count` | 0.35 | `min(log10(median(top10 rating counts) + 1) / 6, 1) × 100` |
| `comp_exact_match` | 0.25 | fraction of the top 10 with the full keyword in title or subtitle, × 100 — title-only in practice, see the caveat above |
| `comp_stars` | 0.10 | `median(top10 average rating) / 5 × 100` |
| `comp_recency` | 0.10 | median days since last update, mapped 0 days → 100, 365+ days → 0 |
| `comp_publisher` | 0.10 | fraction of the top 10 from sellers appearing 2+ times in the result set, × 100 |
| `comp_breadth` | 0.10 | `min(log10(total_result_count + 1) / 2.3, 1) × 100` |

**Missing data is unknown, never zero.** A component that can't be computed is
`None` and the remaining weights are renormalized around it, so a keyword we
know little about doesn't look easy. Within a component, apps missing that
field drop out of the median.

One judgement call worth knowing about: Apple reports `averageUserRating: 0.0`
for apps nobody has rated. That's absent data dressed as a measurement, so
**unrated apps are excluded from the stars median** — counting them would
assert an incumbent is a 0-star app. Rating *counts* of zero are kept, because
"nobody has rated this" is a real observation about review mass.

### Search volume (higher = more volume)

A prefix ladder. For keyword `k`, truncate from the right to build prefixes,
query autocomplete for each, and record:

- **`prefix_depth`** — the shortest prefix at which `k` still appears in the
  suggestion list
- **`hint_rank`** — its position in that list

The reasoning: Apple's autocomplete surfaces high-demand completions early.
A term suggested from two characters in, at rank 1, is one many people type.
A term that only appears once you've typed the whole thing is one nobody
searches. The score is a weighted combination of how short that prefix was and
how high the rank was, with constants in `config.py`
(`SEARCH_DEPTH_WEIGHT`, `SEARCH_RANK_WEIGHT`, `SEARCH_RANK_DECAY`).

The ladder is walked **longest prefix first, descending**, stopping at the
first prefix that fails to surface the keyword. Matching is monotone in
practice — a longer, more specific prefix is likelier to surface the keyword —
so the first miss means nothing shorter can match, and the last hit is the
shortest matching prefix. (The original spec said to walk shortest-to-longest
*and* to short-circuit on the first miss; only the descending walk makes that
short-circuit meaningful, so that's what's implemented.)

Capped at `SEARCH_MAX_PREFIX_QUERIES` (12) requests per keyword; longer
keywords have their ladder sampled evenly, always keeping both ends. Sampling
coarsens `prefix_depth` — the reported depth is the shortest *sampled* prefix
that matched, which can overstate the true one. `LadderObservation.sampled`
records when this happened.

A keyword that never surfaces, even at its full length, scores
`SEARCH_NO_MATCH_SCORE` (1.0) rather than 0 — a true zero would collapse the
opportunity ranking, which multiplies by this number.

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

### Two corrections to the autocomplete request shape

Both found by probing the live endpoint, and both are silent failures rather
than errors:

1. **`X-Apple-Store-Front` is required.** Without it the endpoint returns
   **200 with an empty `hints` array** — which looks exactly like "this
   keyword has no search volume". Every suggestion depends on that header.
2. **The `country` query parameter does nothing.** The storefront comes from
   the header alone; sending `country=us` alongside a German storefront header
   returns German suggestions. `aso.clients.hints.STOREFRONTS` maps ISO codes
   to Apple's numeric storefront ids, and an unknown country **raises** rather
   than falling back to the US — silently researching the wrong market is
   worse than an error.

`clientApplication=Software` does matter: it selects the iOS App Store index.
`MacSoftware`, `iTunes` and `Music` all return music results.

### Rate limiting

The iTunes endpoints start returning 403 above roughly 20 requests/minute per
IP. A single `aso.http.Fetcher` — shared by every client in a run, because the
limit is per IP, not per client — holds all traffic to 15/min via a token
bucket, with an `asyncio.Semaphore(3)` capping requests in flight. 403, 429,
408, 5xx and transport errors retry with exponential backoff and jitter,
giving up after 4 attempts. Genuine 4xx errors are not retried. Nothing is
swallowed: on give-up a `FetchError` propagates so the pipeline can record a
failed-fetch marker rather than aborting the run.

The bucket's burst size (`config.RATE_LIMIT_BURST`) is 1, i.e. strict pacing at
one request every four seconds. That makes the semaphore mostly a backstop —
it starts mattering if you raise the burst.

### Caching

In SQLite, not memory, so a run is resumable — Ctrl-C a long refresh, restart
it, and everything already fetched is free. Raw response bodies are stored
unparsed, so a parser fix can be replayed against captured responses without
going back to Apple.

| What | Where | TTL |
|---|---|---|
| SERP responses | `http_cache` (kind `serp`) | 3 days |
| Autocomplete responses | `http_cache` (kind `hints`) | 3 days |
| App metadata | `apps.fetched_at` | 7 days |

A response that fails to parse is never cached, and a cache hit does not
refresh `apps.fetched_at` — otherwise stale metadata would look freshly
fetched and never be refetched.

**On run time:** at 15 req/min, a 500-keyword cold refresh costs 500 SERP
requests plus roughly 3–6 autocomplete requests each, so ~2,000–3,500 requests,
which is 2–4 hours rather than the 30 minutes in the original spec. 30 minutes
is about right for a *warm* re-run where most responses are still inside their
TTL. Raise `ASO_RATE_LIMIT_PER_MIN` at your own risk.

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
- **`http_cache`** — raw response bodies with a fetch timestamp.

Migrations are an append-only list in `aso/db.py`; `aso init` applies anything
pending. Timestamps are ISO-8601 UTC strings.
