# aso

Self-hosted ASO keyword research for the iOS App Store. Scores keywords on
search volume and competition, and ranks by opportunity.

Single-user, local, no auth. Optimized for being read and modified.

State lives in a handful of small JSON files under `data/` — the tracked
keyword list, the measured observations the fits train on, and the fitted
bridges. There is no database.

> **Build status:** complete. CLI, HTTP API, scoring, Apple Search Ads and
> calibration are all in, with 539 tests.
>
> **No trend history.** Each keyword carries its latest scores, not a series
> of them. `refresh` overwrites. What went with the history: the SERP
> archive, `aso track`, `/movers`, and the per-keyword history endpoint.

---

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # create .venv and install everything
cp .env.example .env    # optional; defaults work without it
uv run pytest
uv run aso version      # shows which data/ is loaded, and how much
```

`uv run` needs no virtualenv activation. If you'd rather type plain `aso`:

```powershell
# Windows / PowerShell — call the executable directly, no activation needed:
.\.venv\Scripts\aso.exe list

# ...or enable activation once (per-user; still blocks unsigned remote scripts):
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
.venv\Scripts\Activate.ps1
aso list
```

```bash
# macOS / Linux
source .venv/bin/activate
aso list
```

A fresh PowerShell install defaults to a `Restricted` execution policy, which
makes `Activate.ps1` fail silently — hence the two alternatives above.

Without `uv` at all:

```bash
python -m venv .venv
pip install -e ".[dev]"
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
  saturates at `limit`, which is 200 — the API's documented maximum.
  `comp_breadth` separates thin niches from crowded ones but still cannot tell
  200 results from 5,000. It was 50 until recently, which pinned the component
  against a ceiling of ~74 for nearly every keyword.

A third source fills the gap the first two leave. `comp_app_power` reads
Apple's **top-charts RSS feeds**, per genre, to ask how big each of the top 10
apps actually is — the "downloads and ranks" factor commercial tools build
difficulty on and that nothing else here could see. Two honest limits:

- **It is ordinal, not a downloads estimate.** The mapping from chart position
  to installs is category- and day-dependent and unknown to us.
- **Coverage is partial by design.** About 40% of top-10 apps chart somewhere.
  The other 60% are genuinely not top-100 apps in their category, which is the
  measurement rather than a hole in it. A *missing index* (every feed failed)
  is different, and makes the component `None` instead of 0.

Similarly, the search score is a **proxy** derived from autocomplete behaviour,
not measured search volume. It stays a proxy until it is calibrated against
real Apple Search Ads impression data. Treat the numbers as ordinal — useful
for ranking keywords against each other, not as absolute demand estimates.
`aso calibrate` fits them against your own measured impressions once you have
connected Search Ads; see _Apple Search Ads setup_ below.

---

## Scoring methodology

Three numbers per keyword per snapshot, all 0–100.

### Competition (higher = harder)

Fetch the SERP for the keyword (200 results), take the top 10, compute eight
normalized components, and combine them as a weighted **power** mean —
`(Σ w·vᴾ / Σ w)^(1/P)`. P > 1 because ranking for a keyword means clearing
every barrier it puts up, not the average one.

Weights and the exponent are **fitted**, not chosen: `aso calibrate-competition`
grid-searches them against a vendor difficulty index with nested
cross-validation. `config.COMPETITION_WEIGHTS` is the authority on the current
values — the table below gives definitions, not weights, precisely so it cannot
drift out of date the way it previously did.

| Component | Definition |
|---|---|
| `comp_rating_count` | `min(log10(median(top10 rating counts) + 1) / 5, 1) × 100` — saturates at a median of 100,000 |
| `comp_exact_match` | mean per-app targeting strength over title + subtitle: full phrase → 1.0, scattered tokens → 0.75 × the fraction present. Title-only in practice, see the caveat above |
| `comp_publisher` | per top-10 slot, `min(log(publisher's slots in the field) / log(4), 1)`, rank-weighted toward the top slots |
| `comp_app_power` | per top-10 slot, `0` if uncharted else `0.35 + 0.65 × (1 − log(best chart rank) / log(101))`, rank-weighted. From Apple's per-genre top-100 charts |
| `comp_stars` | `(median(top10 average rating) − 4.0) / 1.0 × 100`, clamped — the range real SERPs actually occupy |
| `comp_recency` | median days since last update, mapped 0 days → 100, 365+ days → 0 |
| `comp_breadth` | `min(log10(total_result_count + 1) / 2.3, 1) × 100` — saturates at the 200-result request limit, so it only separates thin niches |
| `comp_incumbent` | `sqrt(comp_rating_count × comp_stars)` — derived, recomputed on every rescore |

Several of these currently carry weight 0, for three different reasons that
should not be confused: some are **inputs** to another component
(`comp_rating_count`, `comp_stars` feed `comp_incumbent`), some were **fitted to
zero** and kept measured so a broader sample can overturn that, and
`comp_app_power` is simply **newer than the last fit** and has not been priced
yet. Every one of them is still measured and still stored.

**Missing data is unknown, never zero.** A component that can't be computed is
`None` and the remaining weights are renormalized around it, so a keyword we
know little about doesn't look easy. Within a component, apps missing that
field drop out of the median.

One judgement call worth knowing about: Apple reports `averageUserRating: 0.0`
for apps nobody has rated. That's absent data dressed as a measurement, so
**unrated apps are excluded from the stars median** — counting them would
assert an incumbent is a 0-star app. Rating *counts* of zero are kept, because
"nobody has rated this" is a real observation about review mass.

### Demand (higher = more volume)

Three independent observations, from two different Apple endpoints. Two come
from autocomplete and fail together; the third comes from the SERP and
correlates only **+0.17** with them, which is why combining them works.

**1. `prefix_depth`** — truncate the keyword from the right, query autocomplete
for each prefix, and record the shortest one that still surfaces the keyword.
Apple offers high-demand completions early, so a term suggested from three
characters in is one many people type.

**2. `search_hint_extensions`** — how many of the (at most 10) suggestions for
the keyword's *own full-length prefix* extend it. This costs no extra request:
the full-length prefix is the ladder's first rung.

It exists because the ladder has a structural blind spot. Apple offers
completions and never echoes your query back, so any keyword that is a *stem*
of popular longer terms appears at no rung of its own ladder. `insta` measures
75/100 at AppFigures, is never suggested for any prefix of itself, and has ten
suggestions extending it. On a 182-keyword sample, **52% of keywords surfaced
nowhere in the ladder** and shared one tied floor score — while their true
popularity ranged from 5 to 75. Extensions alone score **+0.473** inside that
tie.

**3. `comp_rating_count`** — the review mass of the top 10, already computed
for the competition score. Apple ranks results by relevance against
popularity, so the apps holding a query's top 10 are an equilibrium outcome:
queries worth winning get won by big apps. Supply reveals demand.

| Component | Weight | Definition |
|---|---|---|
| rating mass | 0.70 | `comp_rating_count` — `log10(median top-10 ratings + 1) / 6 × 100`, capped |
| depth | 0.20 | `SEARCH_DEPTH_DECAY ^ (prefix_depth - 1) × 100` — depth 1 → 100, 2 → 85, 3 → 72, 4 → 61 |
| extensions | 0.10 | `min(extensions, SEARCH_EXTENSIONS_CAP) / SEARCH_EXTENSIONS_CAP × 100` |
| savings | 0.00 | `min(keyword_length - prefix_depth, SEARCH_SAVINGS_CAP) / SEARCH_SAVINGS_CAP × 100` |
| rank | 0.00 | `SEARCH_RANK_DECAY ^ (hint_rank - 1) × 100` |

**These weights are fitted, not chosen.** `aso calibrate` produced them against
182 keywords with AppFigures popularity spanning 5–97. Re-run it when you have
more measured demand; do not hand-edit them and trust this table.

Measured separately against real demand:

| Signal | Rank correlation |
|---|---|
| rating mass | **+0.660** |
| extensions | +0.582 |
| prefix depth | +0.478 |
| hint rank | +0.108 |
| characters saved | −0.030 |
| **all combined** | **+0.779** (0.718 cross-validated) |

**`hint_rank` is dead weight.** It drops out of every blend once rating mass is
present. It is still collected — it comes free with depth — and stored, so a
future sample can revive it, but at weight 0.0 it contributes nothing.

**Rating mass used to be scored backwards.** It was the heaviest *competition*
component at weight 0.35, and since `opportunity = search × (100 -
competition)`, the single best predictor of demand was being spent reducing
the score of the highest-traffic keywords. It now carries demand, and
`COMPETITION_WEIGHTS["comp_rating_count"]` is 0.0 so it is never counted
twice. Competition became the *residual*: given how valuable a query is, how
contested is it?

**Missing components are dropped, not zeroed.** The two instruments fail
independently, so a hints timeout scores the keyword on its SERP alone rather
than dragging it to the floor, and the surviving weights renormalize. A
measured absence is different: a ladder that ran and surfaced nothing scores
those components a real 0.

**Depth is absolute, not relative to keyword length.** An earlier version
divided by `keyword_length - min_len`, which meant a 7-character keyword only
had to survive 5 rungs to hit the ceiling while a 21-character phrase had to
survive 19. That rewarded brevity rather than demand: in a live run it put
"75 hard" at exactly 100.0, tied with instagram, youtube, spotify and netflix.

#### What this proxy still cannot tell you

The two instruments fail in opposite directions, and neither covers the other
completely.

**Autocomplete saturates.** It ranks completions *within a prefix* and says
nothing about how contested that prefix is. `in` is fought over by an enormous
number of queries; `75` is not. A term can win a rare prefix at rank 1 by
default and produce the same reading — depth 2, rank 1 — as a term that won a
ferociously contested one. Rating mass rescues this case, because the two have
very different incumbents.

**The SERP inherits its category.** The mirror failure: a low-volume query
inside a crowded category picks up that category's giants and scores too high.
`finsta` measures 5/100 at AppFigures and scores 92 here, because the apps
returned for it are Instagram-adjacent and enormous.

So read the demand score as *"how much traffic is plausibly behind this
query"*, and expect it to be wrong in both directions on terms where supply
and demand come apart. Only measured impressions settle it — `calibrate()`
against Apple Search Ads.

The ladder is walked **longest prefix first, descending**, stopping at the
first prefix that fails to surface the keyword. Matching is monotone in
practice — a longer, more specific prefix is likelier to surface the keyword —
so the first miss means nothing shorter can match, and the last hit is the
shortest matching prefix. (The original spec said to walk shortest-to-longest
*and* to short-circuit on the first miss; only the descending walk makes that
short-circuit meaningful, so that's what's implemented.)

The ladder runs down to `SEARCH_MIN_PREFIX_LEN` (1) and is capped at
`SEARCH_MAX_PREFIX_QUERIES` (12) requests per keyword; longer
keywords have their ladder sampled evenly, always keeping both ends. Sampling
coarsens `prefix_depth` — the reported depth is the shortest *sampled* prefix
that matched, which can overstate the true one. `LadderObservation.sampled`
records when this happened.

A keyword with *nothing* measurable — no ladder match, no extensions, no SERP
— scores `SEARCH_NO_MATCH_SCORE` (1.0) rather than 0. A true zero would
collapse the opportunity ranking, which multiplies by this number. A keyword
that merely fails to surface in the ladder is no longer at the floor: it still
has extensions and rating mass to be scored on, which is the entire point.

### Opportunity

```
opportunity = search_score × (100 - competition_score) / 100
```

The default sort in the CLI and the API.

### Re-scoring history

Every component that feeds a score is stored on the snapshot row next to the
final number. No score in the database is a bare figure you can't take apart.
That means retuning `COMPETITION_WEIGHTS`, or recalibrating the search mapping
against ASA data, re-scores the entire history without re-fetching anything
from Apple:

```bash
aso rescore
```

It rewrites only the three final scores, from each row's own components and
ladder observations. Components and raw observations are inputs and are never
touched. **Run it after changing any scoring constant**, or trend charts will
splice two different schemes together and invent movement that never happened.

Two cases it deliberately distinguishes: a snapshot whose hints fetch *failed*
has no observations, so its search score stays `NULL` rather than becoming the
no-match floor; a snapshot that was measured and genuinely never surfaced keeps
the floor. Never-measured and measured-as-absent are not the same fact.

---

## Data sources

| Source | Used for | Status |
|---|---|---|
| `itunes.apple.com/search` | competitor set, SERP, app metadata | public, documented |
| `search.itunes.apple.com/.../MZSearchHints.woa` | autocomplete, search proxy | public |
| Apple Search Ads API v5 | ground-truth impressions, calibration | implemented, read-only, opt-in |
| `app-ads.apple.com/reporting/graphql` | Apple's own 5–100 demand index | **undocumented, verified, off by default** |

This project used to promise no undocumented endpoints. That promise was given
up deliberately, for one reason: Apple publishes a keyword popularity index
that every ASO vendor resells, the documented route to it (Impression Share
reports) requires live ad spend, and spending a codebase on *inferring* a
number Apple will simply tell you is a strange trade.

What the old rule bought was reliability, so that is bought back explicitly:

- `ASO_APPLE_POPULARITY_ENABLED` gates every call and defaults to **false**.
  With it off, demand scores are byte-identical to what they were before this
  existed — verified by re-scoring all 231 stored keywords and seeing zero
  move.
- Every failure path falls back to the proxy. An undocumented endpoint changing
  shape must degrade the score, never void it.
- The endpoint's path, payload and parsing live in exactly two places —
  `config.APPLE_POPULARITY_*` and `clients/apple_popularity.py` — so repairing
  it is an edit, not an excavation.

### What the endpoint actually is

Captured live from the dashboard, 2026-08-09. Three things about it contradict
the obvious guess, and all three were guessed wrong first:

- **Not on `api.searchads.apple.com`.** It is on the web dashboard's own host.
- **GraphQL, not REST** — one operation, `getRecommendedKeywordsGql`, under a
  `recommendationV2` root. Schema introspection is disabled.
- **Session cookie, not the ASA bearer token.** The JWT flow in
  `clients/asa.py` has no standing here. This is the awkward part, and it is
  what the two transports below exist to deal with.

### Holding a session: two transports

The endpoint takes a dashboard session and nothing else — no API key, no bearer
token. That leaves an unattractive choice, so both halves of it are
implemented and `ASO_APPLE_TRANSPORT` picks:

| | `cookie` | `browser` |
|---|---|---|
| setup | paste a `Cookie` header into `.env` | `aso apple login`, sign in once |
| dependencies | none | `playwright` + a Chromium download |
| lasts | until Apple expires it | renews itself with use |
| good for | one-off pulls | *"get keyword data any time"* |

`auto` (the default) uses a saved browser profile when one exists and falls
back to the cookie.

```bash
uv sync --extra browser
uv run playwright install chromium
uv run aso apple login          # opens a real window; you sign in
uv run aso apple status         # what's configured, without spending a request
uv run aso apple pull -c us
```

`aso apple login` opens a visible browser and **waits**. Nothing types on your
behalf — Apple sign-in involves a password and usually a second factor, and no
part of this tool should be handling either. The resulting profile lives at
`.apple-session/` and is gitignored, because it holds live session cookies for
an account that can spend money.

**No arrangement here never needs a human again.** Sessions end — password
changes, revoked devices, long absences. When one does, `ApplePopularitySessionExpired`
says so specifically and points at `aso apple login`, rather than surfacing as
a generic endpoint failure that sends you debugging the wrong thing.

### What was ruled out

- **Reading cookies out of your Chrome profile automatically.** Would need the
  browser's credential store and its DPAPI key. Chrome 127+ on Windows uses
  App-Bound Encryption specifically to stop other processes doing this, and it
  is indistinguishable from credential theft besides.
- **The ASA bearer token.** Different host, different auth system. Worth a
  two-minute test if you have API credentials handy — if it worked, both
  transports above would be unnecessary — but do not expect it to.

It is a *recommendation* tool, not a lookup: one seed returns ~100 related
keywords with popularity. Reading a keyword's own popularity means asking for
it and finding it in its own results — and Apple omitting it is the censoring
signal the blend consumes.

**The bycatch is worth more than the catch.** Asking about 231 tracked keywords
returned **2,658 additional keywords** with Apple popularity attached, for no
extra request. That is keyword discovery and demand measurement in one call.

### What it changed here

Measured on 183 overlapping keywords:

| Graded against | Spearman |
|---|---|
| AppFigures popularity | +0.779 |
| **Apple's own index** | **+0.718** |

The proxy scores *worse* against Apple than against AppFigures — which is
exactly what this README's third calibration caveat predicted. Part of the
0.779 was agreement with AppFigures' estimator rather than with reality. That
caveat is now settled with a number instead of a worry.

Both documented failure cases were corrected by measurement:

| keyword | proxy said | Apple says | the failure |
|---|---|---|---|
| `75 hard` | 70.7 | **9** | autocomplete saturation |
| `finsta` | 92.3 | **8** | SERP inherits its category |
| `insta` | 80.0 | 79 | (proxy was right) |
| `facebook` | 100.0 | 96 | (proxy was right) |

`finsta` moved 43.4 opportunity points — the largest single correction in the
re-score, on precisely the keyword named above as the worst case.

---

## Calibration: teaching the proxy from measured demand

The search score is inferred from autocomplete. **Calibration** fits its
constants against demand somebody actually measured, so the ordering stops
depending on constants I picked by hand.

Any source works, as long as it can be reduced to *(keyword, country) → a
number*. Two are supported today:

| Source | Scale | Needs | Coverage |
|---|---|---|---|
| **AppFigures** keyword export | `ordinal_100` — a 0–100 popularity rank | an AppFigures subscription | any keyword you can look up |
| **Apple Search Ads** search terms | `count` — raw impressions | real ad spend | only terms your ads won |

### Why `scale` matters, and is stored per row

Impression counts are unbounded and roughly log-distributed, so `calibrate()`
takes `log10` before normalizing — otherwise three head terms dictate the whole
fit. A popularity rank is *already* a bounded 0–100 ordering, and taking its
log would compress the top of the scale and **distort the very ordering being
fitted**, which is the only thing that target is good for.

So each observation records its scale, and `calibrate()` refuses to fit a mix
of scales in one run. Impressions and popularity ranks are not commensurable
and averaging them yields a number that means nothing.

### Importing a keyword export

```bash
aso import-demand ~/Downloads/related_keywords_75_hard-ios-handheld-us.csv     --source appfigures --country us --track
aso refresh          # every keyword needs a ladder observation to fit against
aso calibrate
```

**`--country` is a flag, not a column, and it is required.** These exports do
not carry a storefront — AppFigures puts it in the *filename*, which is far too
fragile to parse. Getting it wrong would silently calibrate US keywords against
German demand, so it is explicit.

`--track` also adds each imported keyword to your tracked list, which is what
makes `aso refresh` produce the ladder observations the fit joins against.
Without observations there is nothing to calibrate, however much demand data
you have imported.

### Calibrating the competition half

The same AppFigures file carries a **Competitiveness** column beside the
Popularity one. `aso import-demand` reads the first, `aso import-competition`
reads the second, and they are separate commands because the two columns
disagree about which rows are usable — a keyword can be rated for difficulty
and blank for popularity, and a combined reader would have to drop both.

```bash
aso import-competition ~/Downloads/related_keywords_75_hard-ios-handheld-us.csv \
    --source appfigures --country us --track
aso refresh                  # every keyword needs a SERP capture to fit against
aso calibrate-competition
```

Same contract as `aso calibrate`: it prints constants and does not write them.

Two differences from the demand fit worth knowing:

**Zero is kept.** `import-demand` drops rows with zero demand, because zero
demand is usually a reporting floor rather than a measurement. Zero difficulty
is a measurement — it is the easy end of exactly the range the fit needs.

**Components too rarely measured are excluded from the grid, not given zero.**
`combine()` skips a `None` component and renormalizes, so a weight vector that
loads mass onto a column which is null everywhere scores *identically* to one
that ignores it — and being an equal best, it can win and be reported. The fit
would then be handing weight to a component it never evaluated. Any component
present on under half the samples is kept out of the search and named in the
output, because "the fit rejected this" and "the fit never saw this" are
different findings and only the first is evidence.

### `--stratify N`, and why spread beats volume

```bash
aso import-demand export.csv --stratify 25
```

Imports every row of demand (free — no network) but tracks only 25 keywords,
chosen to span as many distinct demand levels as possible.

This matters because **each tracked keyword costs a full prefix-ladder walk
against Apple** — roughly a minute at the rate limit — and because a vendor
export is dominated by long-tail terms sitting at the vendor's floor value.
Importing one export of 101 keywords and refreshing all of them produced 26
usable samples of which **21 shared the single value 5**: nearly 40 minutes of
requests spent to learn almost nothing about ordering.

Note this samples across distinct *values*, not across rank positions. Even
spacing over rank preserves the input distribution — 90% floor in, 90% floor
out — so it round-robins across demand levels instead, taking one keyword from
each before a second from any.

The practical recipe: export from **several seed keywords of different
popularity** (a broad head term, a mid term, your niche), import each with
`--stratify`, then refresh.

### Guards that stop a confident but meaningless fit

| Guard | Refuses when |
|---|---|
| `NotEnoughData` | fewer than `CALIBRATION_MIN_SAMPLES` (20) keywords join |
| `DegenerateSample` | fewer than 6 distinct target values, or >50% share one value |
| grid-edge warning | a fitted constant pins to the end of its search grid |

The second exists because of the run described above: enough rows to look
respectable, almost no ordering to fit, and a Spearman number that would have
been noise dressed as a result.

The third is a warning rather than a refusal. A constant landing on the edge of
`CALIBRATION_*_GRID` means the best value is probably *outside* the range
searched, so the fit is a boundary artefact — widen the grid in `config.py` or
treat the result as provisional.

### Selection optimizes the metric it reports

The three weights are solved in closed form by least squares, but *choosing*
between grid points is done on **Spearman rank correlation**, tie-broken by
RMSE. Selecting on RMSE while reporting Spearman is not a theoretical concern:
the first real run returned a fit scoring 0.564 against the incumbent
constants' 0.579 — worse than what it was meant to replace, because it was
optimizing one thing and being graded on another.

Rows with a blank or unparseable value are **skipped and reported**, never read
as zero: a keyword we could not read a number for is not a keyword with no
demand.

### An honest word on what this calibration proves

AppFigures' Popularity is itself derived from Apple's own keyword popularity
indicator — an ordinal demand ranking, not raw impressions. Fitting against it
moves our proxy one rung closer to Apple; it does not make it a measurement.

Concretely, after calibrating against AppFigures the search score means
*"ranks roughly where AppFigures ranks this"* — a defensible, checkable claim,
and a real improvement on *"ranks where my hand-picked constants say"*. It
still does not mean a number of searches. Only the ASA `count` path could
support that, and only for keywords your ads have won.

`aso calibrate` prints the rank correlation of both the new fit **and** your
current constants, so you can see whether calibrating actually helped rather
than assuming it did.

**Where it currently stands.** Fitted on 182 keywords spanning popularity
5–97, the demand score reaches a cross-validated Spearman of **0.718** and an
in-sample **0.779**, against **0.487** for the previous depth-and-rank model.
Roughly half the variance, and still not a demand measurement — keywords a few
points apart are indistinguishable; only large gaps carry meaning.

The jump did not come from better fitting. Reweighting the old two-signal model
was exhausted: an *oracle* mapping — every distinct `(depth, rank)` state
replaced by its true mean popularity, fitted in-sample with no honesty penalty
— tops out at **0.557**. The gain came from adding instruments, one of which
(rating mass) the tool was already collecting and scoring in the wrong
direction.

Three caveats about that number, the first two of which cut against it:

- **Range matters more than sample size.** The same mapping measured only 0.169
  on an earlier 54-keyword sample that topped out at popularity 67 and had 81%
  of its rows at the vendor's floor value. Nothing about the proxy changed —
  the sample simply could not grade it. If your export is all long-tail, expect
  a pessimistic and noisy result.
- **The head of the current sample is brand-concentrated.** It came from an
  "instagram related keywords" export, so the high-popularity end is mostly
  misspellings and transliterations of one mega-brand. Stripping all 37 brand
  and non-Latin terms and re-measuring on the remaining 145 generic keywords
  gave **0.733** against 0.735 for the full set, so the signal is not a brand
  artefact — but confirm with a second export seeded from a head term in your
  own vertical before trusting the score across demand tiers.
- **The target may not be independent of the predictor.** AppFigures'
  popularity is a model, not Apple's ground truth. If their model also reads
  SERP app strength, part of the 0.718 is agreement with their estimator
  rather than with reality. Only ASA impressions can settle this, and they
  only cover keywords your ads have won.

---

## Apple Search Ads setup

Entirely optional. Everything except `aso asa …` and `aso calibrate` works
without it. Connect it when you want the search score fitted against
impressions you actually measured rather than inferred.

### 1. Generate a key pair

Since API v4 Apple no longer issues you a `.p8` — you make the key, Apple only
ever sees the public half:

```bash
openssl ecparam -genkey -name prime256v1 -noout -out asa-private-key.pem
openssl ec -in asa-private-key.pem -pubout -out asa-public-key.pem
```

`*.pem` and `*.p8` are gitignored. Keep the private key out of the repo and
out of backups you share.

### 2. Register the public key

In [Search Ads](https://searchads.apple.com) → **Account Settings → API** →
create an API user. Paste the contents of `asa-public-key.pem`.

Give it the **API Account Read Only** role. This tool never creates, modifies
or spends anything, and a research tool has no business holding a credential
that can move your budget.

Apple then shows `clientId`, `teamId` and `keyId` **once**. Copy them
immediately.

### 3. Fill in `.env`

```
ASO_ASA_CLIENT_ID=SEARCHADS.xxxxxxxx-...
ASO_ASA_TEAM_ID=SEARCHADS.xxxxxxxx-...
ASO_ASA_KEY_ID=xxxxxxxx-...
ASO_ASA_ORG_ID=1234567
ASO_ASA_PRIVATE_KEY_PATH=./asa-private-key.pem
```

### 4. Verify, pull, calibrate

```bash
aso asa whoami        # checks credentials; prints every org they can reach
aso asa campaigns     # which campaigns exist, and which can be calibrated against
aso asa pull --days 90
aso calibrate
```

`whoami` is the cheapest possible check — one token exchange and one GET — and
it prints your org ID, so run it first if `ASO_ASA_ORG_ID` is the value you are
unsure about.

### How authentication works

A short-lived ES256 JWT (the *client assertion*) is signed with your private
key and exchanged at `appleid.apple.com` for a bearer token, which is cached in
memory for its lifetime and never written to disk. The assertion goes in the
POST **body**, not the query string: Apple's own example puts it in the URL,
which contradicts the `x-www-form-urlencoded` content type it also sets, and a
signed credential in a URL gets logged by httpx, by any proxy, and by Apple's
access logs. If Apple rejects a form body with a 400, the client retries once
using their documented query-string form and logs a warning.

### What the data can and cannot support

A search-terms report shows what people typed **that reached your ads** —
filtered by your targeting, your budget, and the auction. A term with zero
impressions is not a term nobody searches; it is one you did not bid on or did
not win.

So calibration is fitted to the corner of the App Store your campaigns can
see. `aso calibrate` is explicit about this: it prints the sample size,
refuses to fit fewer than `CALIBRATION_MIN_SAMPLES` (20) keywords, and reports
the rank correlation of both the new fit **and** your current constants, so
"did this actually help?" is answerable rather than assumed. If the fit does
not beat what you already have, it says so and tells you to keep it.

Campaigns targeting more than one country are pulled but stored with a NULL
country and excluded from the fit — a search term cannot be attributed to one
of several storefronts, and calibration is the one place where a plausible
guess defeats the entire purpose.

### Applying a fit

`aso calibrate` prints constants; it does **not** write them. Paste them into
`aso/config.py`, then run `aso rescore` to move history onto the new mapping.
A scoring constant that rewrites itself is one nobody reviews.

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

In memory, one cache per process. Raw response bodies are held unparsed, so
a repeat lookup inside the TTL costs nothing.

| What | Kind | TTL |
|---|---|---|
| SERP responses | `serp` | 3 days |
| Autocomplete responses | `hints` | 3 days |
| Chart feeds | `charts` | 1 day |

A response that fails to parse is never cached — otherwise one bad body
poisons the keyword for three days.

**This used to be on disk, and the change has a cost.** The `http_cache`
table was 459MB of the old 492MB database, mostly SERP bodies kept long past
the TTL that made them meaningful. Moving it in-process reclaimed all of
that and gave up resumability: a CLI run that dies partway now restarts from
nothing, and re-pays the 48 requests for a chart index. The long-lived
caller — the API server — keeps its cache warm all day, which is where the
caching actually mattered.

**On run time:** at 15 req/min, a 500-keyword cold refresh costs 500 SERP
requests plus roughly 3–6 autocomplete requests each, so ~2,000–3,500 requests,
which is 2–4 hours rather than the 30 minutes in the original spec. 30 minutes
is about right for a *warm* re-run where most responses are still inside their
TTL. Raise `ASO_RATE_LIMIT_PER_MIN` at your own risk.

---

## CLI

```
aso check "meditation timer"            # score once, track nothing
aso add "candlestick patterns" --country us --tag lcp
aso import keywords.csv                 # needs a `keyword` column
aso refresh --tag lcp --country us      # run the pipeline, record the scores
aso rescore                             # recompute every score, no network
aso list --sort opportunity --limit 30
aso show "candlestick patterns"         # scores and every component
aso export --format csv -o exports/keywords.csv

aso asa whoami                          # verify ASA credentials, list orgs
aso asa campaigns
aso asa pull --days 90                  # measured impressions into data/

aso import-demand keywords.csv --source appfigures --country us --track
aso calibrate --source appfigures       # fit the search mapping against demand
```

`check` answers *"is this term worth tracking?"* without committing to it. It
runs exactly the same scoring code as `refresh` — the two share one
implementation, and a test asserts they produce identical numbers for the same
keyword — but records nothing; use `aso add` for anything you want to keep a
score for. It does populate the response cache, which is a cache rather than
a record, and makes a later `refresh` of that keyword free within the TTL.

Every command creates `data/` on demand, so there is nothing to initialise.

`refresh` also takes `--keyword` for a single term, `--limit` to cap a run,
`--force` to ignore the caches, and `--verbose` to log every HTTP request.
**Ctrl-C loses the run.** The keyword list is one JSON file, written once when
the run finishes, so a refresh interrupted partway records nothing and the
in-memory response cache dies with the process. That is the trade for not
holding a database open; use `--limit` to break a long run into pieces you can
afford to lose.

Keywords are stored lowercased and whitespace-collapsed. "Day Trading" and
"day trading" are one tracked keyword, not two — App Store search is
case-insensitive, and storing both would double the refresh cost for one
keyword's worth of information.

`import` takes a CSV with a `keyword` column, plus optional `country` and
`tags` (comma- or semicolon-separated). Re-importing merges tags rather than
erroring, so an overlapping file is safe to re-run.

`export` writes every component alongside the final scores, so a spreadsheet
can re-weight them without touching this tool.

## API

```bash
uv run aso serve            # http://127.0.0.1:8081
```

Interactive docs at `/docs`. See `deploy/` for running it under systemd.

### It binds loopback, and it is the only thing that fetches

Two properties, and the second is the one that is easy to get wrong.

**Loopback**, because this tool has no authentication by design. Other services
on the host can reach it; nothing else can. From a laptop, use an SSH tunnel.

**The only fetcher**, because `aso.http.Fetcher`'s token bucket lives in one
process. Two processes on one IP means two buckets, roughly 30 req/min against
a limit that starts returning 403 around 20. So the API owns a single `Fetcher`
for its lifetime, and the nightly timer POSTs to `/refresh` rather than running
`aso refresh`. The CLI still works — just don't point its fetching commands at
Apple while the API is up.

That single shared bucket is also why a live `/lookup` arriving during a
three-hour refresh returns in seconds rather than waiting for it: the two
interleave request-by-request instead of queueing behind a lock.

### Endpoints

| | |
|---|---|
| `GET /health` | data dir, keyword count, observation and bridge counts |
| `GET /keywords` | `country`, `tag`, `keyword`, `sort`, `limit`, `include_inactive`, `include_unscored` |
| `GET /keywords/{id}` | latest scores, components with weights |
| `GET /tags`, `GET /countries` | |
| `POST /keywords` | add; re-posting merges tags |
| `PATCH /keywords/{id}` | `active`, `tags` (replaces) |
| `DELETE /keywords/{id}` | permanent; returns what it removed |
| `POST /lookup` | score any keyword live |
| `POST /refresh` | 202 + job id |
| `POST /asa/pull`, `POST /popularity/pull` | 202 + job id |
| `POST /rescore` | synchronous |
| `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel` | |

Keywords are addressed by id in paths because they contain spaces, unicode, and
sometimes `/`. `GET /keywords?keyword=…&country=…` is how a caller holding only
the string finds the id.

Every response carrying a score also carries `captured_at`. A machine that
cannot tell a fresh score from a three-week-old one will treat stale data as
current.

### `/lookup` is cached, and pays for the chart index

It reads through the server's in-process response cache — SERP and autocomplete
at a 3-day TTL — so a repeat lookup inside that window makes zero requests and
returns in milliseconds. `requests_made` in the response tells you which
happened; `"force": true` bypasses it. Stored scores are never read, so a
change to `COMPETITION_WEIGHTS` shows up on the next call either way.

Because the cache is per-process, a server restart empties it: the first
lookup after a deploy pays full price again.

Unlike `aso check`, it supplies the storefront chart index, so `comp_app_power`
is present and the competition score is comparable to a tracked keyword's.
That costs ~48 requests once per storefront per day — meaning the first lookup
of the day can take about four minutes if the nightly refresh has not already
warmed it. Set your client timeout accordingly.

### Jobs are in memory

A job is an asyncio task in the API process. Restart the service and running
jobs are cancelled and their records lost — the run's real record was always
the scores in `data/keywords.json`, plus journald. The registry keeps the
last 50.

One job per kind: a second `POST /refresh` while one is running gets 409, since
two runs scoring an overlapping set is simply wrong. A refresh and an ASA pull
together are fine.

`POST /jobs/{id}/cancel` returns the job already marked `cancelled`, with its
partial `done` count intact — the route yields to the event loop once so the
cancelled task can stamp its own terminal status before the response is built.
A cancelled refresh writes nothing: the keyword list is saved when the run
finishes, so the scores it had already computed are lost with it.

`POST /refresh` rejects `limit` below 1. Zero would select nothing and then
report "no keywords match that filter", which is false — keywords matched, the
limit discarded them.

**Run one worker.** `aso serve` has no `--workers` flag on purpose: two workers
would mean two token buckets and two job registries behind one URL.

---

## Data model

Five JSON files under `data/`, about 740KB in total. `country` is a real field
everywhere anything varies by storefront — nothing defaults to `us`.

| File | What | Size |
|---|---|---|
| `keywords.json` | what's tracked, each with its latest scores, every component and a failed-fetch marker. Unique on `(keyword, country)`. | grows with use |
| `demand_observations.json` | what a source measured a keyword's demand to be | 3,120 rows |
| `competition_observations.json` | what a vendor rated its difficulty | 421 rows |
| `bridges.json` | the fitted maps from this tool's scale onto theirs | 2 |
| `calibration_corpus.json` | frozen component rows the fits train on | 231 rows |

Records are written one per line so `git diff` names the row that changed, and
writes go through a temp file and a rename so an interrupted write cannot
truncate the list.

`aso/store.py` owns the keyword list and `aso/calibration.py` owns the rest, so
file layout doesn't leak into the CLI, the API or the pipeline.
`aso/pipeline.py` orchestrates a refresh.

### Why the corpus is frozen, and separate

Every fit needs `(observation, components)` pairs, and the component half used
to come from the latest snapshot of a tracked keyword. Clearing the keyword
list would therefore have emptied the training set and silently changed every
fitted constant in `config.py` — the numbers would still be there, but nobody
could re-derive them.

So the component side is frozen once: 231 keywords as they scored on
2026-08-09, independent of what you happen to track now. The fits read the
corpus and the live keyword list together, live winning on `(keyword,
country)`. Today's fits reproduce exactly against an empty list, and anything
you track and refresh from here on joins the training set on its own.

### How a failed fetch is recorded

A keyword whose SERP or hints can't be fetched still gets its record updated,
with `fetch_failed = 1`, the error text, and whatever *did* succeed. The run
continues — a 500-keyword refresh must not die on keyword 300.

Partial results stay partial. If the SERP succeeds and hints fail, the
competition score and its components are written and `search_score` stays
null — never zero, never a guess. `opportunity_score` is null whenever either
input is, so a half-measured keyword can't outrank a fully measured one, and
unscored keywords sort last in every view rather than first.

A missing file reads as empty, so a fresh install needs no setup step. A file
that exists but holds the wrong shape raises rather than reading as empty —
silently treating a broken install as "no data" would discard measurements and
then write the emptiness back. Timestamps are ISO-8601 UTC strings.
