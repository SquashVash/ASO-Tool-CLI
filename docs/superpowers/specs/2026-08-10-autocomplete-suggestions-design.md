# Autocomplete suggestions: keyword discovery from Apple's own completions

**Status:** approved, not yet implemented
**Date:** 2026-08-10

## Why

The tool can price a keyword you name and cannot tell you which keywords to
name. Every candidate has to come from somewhere else — a competitor's listing,
a guess, a vendor export. Meanwhile the scorer already asks Apple, up to twelve
times per keyword, "what do people search that starts with this?", and throws
every answer away after extracting three integers.

The suggestion lists are the most direct statement of demand Apple gives out.
Discovery is reading them instead of discarding them.

## What this is not

**It does not score candidates.** Scoring forty suggestions is forty SERP
fetches plus forty prefix ladders — hours at the paced rate. `suggest` returns
names; `POST /lookup` and `aso check` price the ones worth pricing. Keeping the
two apart is what keeps discovery cheap enough to run casually.

It also needs no chart index, so it avoids the ~48-request penalty `/lookup`
pays on the first call of the day in a storefront.

## The ladder, and why this walks it differently

`scoring/search.observe()` walks prefixes longest-first and **stops at the first
miss**: once the keyword itself no longer appears in its own suggestions,
nothing shorter will surface it either, so probing on would buy the score
nothing.

That optimization is wrong for discovery, and wrong in the worst direction. The
keywords most worth researching are the ones Apple never echoes back — the
module's own example is `insta`, which has real demand and whose ladder stops
after a single probe. Reusing `observe` would return the fewest candidates
exactly where the most are needed.

So discovery shares `prefix_lengths()` — the function that *defines* what the
ladder is — and probes every rung it returns. The rung arithmetic stays in one
place. The early stop stays where it belongs, in scoring.

This is the load-bearing decision in this document. A future reader tempted to
"reuse `observe` and avoid the duplication" should note there is no duplication:
the shared part is already shared, and the unshared part is a difference in
purpose, not an oversight.

## Architecture

`aso/suggest.py`, mirroring `aso/lookup.py`:

- `suggest_async(keyword, country, *, force, fetcher, store)` — the core. The
  API awaits it directly so it can hand over the one `Fetcher` its process
  owns, rather than opening a second token bucket against the same IP.
- `suggest(keyword, country, *, force)` — `asyncio.run` wrapper for the CLI.

One implementation, two callers: the arrangement `aso check` and `POST /lookup`
already share through `lookup.py`.

### Result shape

```python
@dataclass(frozen=True)
class Candidate:
    term: str
    prefix: str        # the DEEPEST prefix that still surfaced this term
    rank: int          # its 1-based position in that prefix's list
    surfaced_by: int   # how many rungs mentioned it at all
    tracked: bool

@dataclass(frozen=True)
class SuggestResult:
    keyword: str
    country: str
    candidates: list[Candidate]
    prefixes_probed: list[str]
    requests_made: int
    failed: bool
    error: str | None
```

### Ordering

Default sort: **deepest prefix first, then rank ascending.**

> **Corrected after first run.** This section originally specified the
> *shortest* prefix, reasoning that it restates what `SEARCH_DEPTH_WEIGHT`
> (0.30, fitted) claims: a term offered after three characters carries more
> demand than one needing eleven. That reasoning is sound and it answers the
> wrong question.
>
> Running `aso suggest habit` under the original rule returned, in order:
> `hinge`, `hbo max`, `hoopla`, `hulu`, `home depot`. All surfaced at prefix
> `"h"`, all genuinely high-demand, none remotely about habits — while
> `habit tracker`, `habitica` and `habitify` sat at the bottom.
>
> On a prefix ladder, demand and relevance are in direct opposition. The
> shallow rungs return whatever is most popular in the entire storefront,
> because that is what Apple completes a single letter with. Discovery needs
> relevance, so depth is inverted here relative to scoring.

A term still being offered once you have typed five characters is about your
seed; one that died at `"h"` shares a letter with it. Within a single list
Apple's order *is* the demand signal, so rank breaks ties — but across two
different prefixes raw rank is not comparable, and the API documents that.

### Deduplication

A term surfacing at several rungs collapses to one candidate keeping the
**deepest** prefix and that prefix's rank. Since rungs run longest-first, that
is simply the first sighting.

`surfaced_by` preserves the discarded information: a term offered at nine of
twelve rungs is a stronger signal than one offered at a single rung, and
collapsing without counting would throw that away silently.

## Tracked terms are excluded by default

`include_tracked` defaults to `False`. Discovery answers "what am I missing",
and terms already on the list are noise in that answer.

The cost is real and worth stating: with the default on, a tracked keyword that
has **dropped out** of Apple's suggestions entirely is indistinguishable from
one that never appeared. `include_tracked=true` restores the full picture, and
every candidate carries `tracked` either way so the filtered field is never
guesswork.

Matching is on `store.normalize_keyword`, the same normalization the keyword
list stores under, so case and whitespace cannot cause a false "new".

## Failure handling

A hints failure part-way down the ladder returns the candidates already
collected, with `failed=True` and the error text — the pipeline's "partial
results stay partial" rule. Eight rungs of suggestions and an honest error beats
an exception that discards them.

Raised *before* any request, as 422 on the API and exit 1 on the CLI:

- a blank keyword
- `UnknownStorefront` for a country with no Apple storefront id

## Surfaces

**`POST /suggest`** — POST rather than GET for the same reason `/lookup` is one:
keywords contain spaces, unicode and sometimes `/`, none of which are pleasant
in a query string or path.

```json
{ "keyword": "habit tracker", "country": "us",
  "force": false, "include_tracked": false }
```

**`aso suggest "habit tracker"`** with `--country`, `--json`, `--force`,
`--include-tracked`, `--verbose`. Table columns: term, prefix, rank, seen
(`surfaced_by`), and a `tracked` marker when tracked terms are included.

Both remain synchronous. `/lookup` already blocks up to four minutes building a
chart index; under a minute needs no job registry.

## Cost

`min(len(keyword), SEARCH_MAX_PREFIX_QUERIES)` requests — at most 12 — at the
paced ~4s each:

| keyword | requests | cold |
|---|---|---|
| `habit` (5) | 5 | ~20s |
| `habit tracker` (13) | 12 | ~48s |

Free on repeat within the 3-day hints TTL. Because the cache is per-process,
the CLI and the server do not share it.

Documented on both surfaces so nobody sets a 10-second client timeout.

## Testing

- **Collector**, against a fake probe, no network: dedup keeps the shortest
  prefix; `surfaced_by` counts every rung; ordering is depth-then-rank; a
  probe raising mid-walk yields partial candidates plus the error; tracked
  terms are excluded by default and returned with `include_tracked`.
- **A keyword no prefix surfaces** still returns candidates — the `insta` case,
  and the direct regression test for reusing `observe`'s early stop by mistake.
- **`POST /suggest`** under respx: shape, 422s, `include_tracked`.
- **`aso suggest`**: table and `--json`.

No new fixtures; `tests/fixtures/hints_candlestick_us.plist` and
`hints_empty_us.plist` already cover both response shapes.

## Out of scope

Scoring candidates; multi-round expansion (probing each suggestion in turn);
persisting suggestions; suggestion history or diffing over time. Each is a
separate decision with its own cost, and none is needed to answer "what should
I be tracking?"
