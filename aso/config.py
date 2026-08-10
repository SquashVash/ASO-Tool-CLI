"""Settings, loaded from .env with sane defaults.

Everything tunable lives here so scoring logic never has a magic number baked
into it. Import the module-level `settings` singleton; call `reload()` in tests
or a REPL if you change the environment underneath it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# Competition weights
# ---------------------------------------------------------------------------
# Six normalized 0-100 components combined as a weighted mean. Tune these
# freely: every component is stored on the snapshot row, so changing a weight
# lets you re-score history without re-fetching anything.
#
# Keys must match the `comp_*` column names on `snapshots`.
#
# FITTED, not chosen. Re-derived 2026-08-09 by `aso calibrate-competition`
# against 207 keywords carrying AppFigures Competitiveness ratings, selected by
# grid search over the weight simplex and the aggregation exponent, scored by
# rank correlation and validated with nested 5-fold cross-validation.
#
#   hand-chosen weights (before any fitting)           0.558
#   first fit, before comp_app_power existed           0.772
#   this fit, cross-validated                          0.814
#
# In-sample was 0.815 against a cross-validated 0.814. That gap is normally the
# size of the overfit you would otherwise have believed; here there is
# essentially none, which is the strongest evidence in this file that the model
# is fitting signal rather than the sample.
#
# Re-derive with `aso calibrate-competition`. Do not hand-edit and expect the
# numbers above to still describe reality.
#
# WHAT THE LATEST FIT FOUND
# -------------------------
# `comp_app_power` — how big the top 10's apps are, from Apple's per-genre top
# charts — took 0.625, more than every other component combined. It is the
# heaviest term in the model by a wide margin, on its first appearance.
#
# That is the diagnosis this whole revision was chasing. Every other component
# reads text or reputation around a keyword; none of them could tell a
# category leader from a well-reviewed small app, and that distinction turns
# out to be most of what "difficult" means. Appfigures names downloads and
# ranks first among its own inputs and AppTweak reduces difficulty entirely to
# the top 10's "App Power" — both were describing the term we did not have.
#
# `comp_publisher` fell from 0.50 to 0.125 as a direct consequence. Its old
# weight was fitted on a sample of four topic clusters with no app-size term to
# compete against, and concentration was standing in for size: the publishers
# holding many slots in a field tend to be the big ones. Given the real thing,
# the proxy is worth a quarter of what it was.
#
# The aggregation exponent fell from 3.0 to 2.0 for a related reason. With a
# component that genuinely spikes on hard keywords, the model no longer needs a
# steep power to keep hard keywords from being averaged down.
#
# WHAT EARLIER FITS OVERTURNED, AND STILL HOLDS
# ---------------------------------------------
# `comp_incumbent` — sqrt(review mass x stars) — was once the heaviest term at
# 0.45, on the reasoning that displacing an incumbent is hard when it is *both*
# well reviewed and heavily reviewed. It scores 0.589 against AppFigures while
# the raw review mass inside it scores 0.783. Multiplying by stars destroys
# signal rather than adding it, because `comp_stars` carries none: its rank
# correlation with difficulty is -0.091, and its p05-to-p95 spans 4.57 to 4.84
# stars. Nearly every app in a top 10 is well rated, so the factor is close to
# a constant, and an interaction with a constant is a rescale wearing a costume.
#
# `comp_breadth` carried 0.10 while correlating -0.424 — it was pushing the
# score the wrong way. Read that number with saturation in mind: `resultCount`
# caps at the request limit, so the component spanned 71.9 to 74.2 from p25 to
# p95 and could not correlate with anything. ITUNES_SEARCH_LIMIT has since gone
# 50 -> 200, which moves the saturation point without removing it (see
# COMP_BREADTH_LOG_DIVISOR for post-change measurements). Weight 0 survived the
# re-fit, and now rests on a measured reason rather than a broken range.
#
# Weight 0 means `competition.combine` skips the component and renormalizes
# over the rest, while `pipeline` and `dashboard` still treat every key here as
# a snapshot column — which all eight are. The zeroed components stay measured
# and stored so that a later, broader sample can overturn this in turn.
#
# WHAT THIS FIT DOES NOT PROVE
# ----------------------------
# AppFigures Competitiveness is a vendor's derived index, not ground truth.
# These weights reproduce AppFigures. That is a real improvement over
# reproducing nothing and it is not the same as being right.
#
# Nor does any of this fix the *level*. Rank correlation is invariant under any
# monotone transform of the output, so the procedure that chose these weights
# is structurally blind to whether the whole column sits twenty points low.
# That is a separate job with a separate tool — see `aso bridge-competition`
# and `competition.fit_bridge`.
COMPETITION_WEIGHTS: dict[str, float] = {
    "comp_app_power": 0.625,  # chart rank of the top 10 — the heaviest term
    "comp_publisher": 0.125,  # concentration: one seller holding many slots
    "comp_rating_count": 0.125,  # review mass of the top 10
    "comp_exact_match": 0.125,  # how squarely the incumbents target the term
    "comp_stars": 0.00,  # measured, stored, and worth nothing here (rho -0.09)
    "comp_incumbent": 0.00,  # superseded by comp_rating_count alone
    "comp_recency": 0.00,  # drops out once the weighted components are in
    "comp_breadth": 0.00,  # saturated at the request limit at any limit
}

# --- component normalization constants -------------------------------------
# comp_rating_count: log10(median + 1) / DIVISOR, clamped to 1.
# 5 means "a median of 100,000 ratings in the top 10 saturates the component".
#
# It was 6 (a million). Measured over 397 stored snapshots the median top 10
# had 10,948 ratings and the 75th percentile had 61,682, so under the old
# divisor the typical keyword scored 67 and the whole span from 100k to 1M --
# a range almost nothing occupies -- absorbed the top third of the scale. A
# top 10 whose median app carries 100,000 reviews is already about as
# entrenched as the App Store gets, so that is where saturation belongs.
#
# Not lowered further: at 4.5 more than a third of stored keywords pin at 100
# and the component stops discriminating. At 5.0 only 63 of 397 pin, and the
# spread widens (sd 19.6 -> 21.5) rather than collapsing.
COMP_RATING_COUNT_LOG_DIVISOR = 5.0

# comp_breadth: log10(total_result_count + 1) / DIVISOR, clamped to 1.
# 2.3 means "~200 total results saturates", which is the API's maximum `limit`
# and therefore the largest `resultCount` it can report.
#
# The divisor was always written for a 200-result field. What changed is that
# we now ask for 200 (see ITUNES_SEARCH_LIMIT); while the request limit was 50
# the component could not exceed 74.
#
# BE HONEST ABOUT WHAT THIS FIXED
# -------------------------------
# Not the saturation. Measured on five live keywords after the change, the
# component reads 95.7, 97.5, 98.3, 98.8, 99.4 — most keywords match more than
# 200 apps, so `resultCount` still comes back pinned at the request limit and
# the component is still close to a constant. Widening the request moved the
# saturation point from "50 results" to "200 results"; it did not remove it.
#
# So this measures one thing: whether a keyword has fewer than ~200 matching
# apps at all. That is a genuine signal for thin niches and silence everywhere
# else, which is roughly what the -0.424 correlation was picking up. Expect a
# re-fit to leave it at weight 0, and read that as a fact about `resultCount`
# rather than a fact about crowding.
#
# Distinguishing crowded from enormous needs a source that reports a true
# total. This endpoint does not have one at any limit.
COMP_BREADTH_LOG_DIVISOR = 2.3

# comp_recency: linear map from days-since-update to score.
# 0 days -> 100 (actively maintained, harder), 365+ days -> 0 (stale, easier).
COMP_RECENCY_MAX_DAYS = 365.0

# comp_stars: median average rating, mapped from [MIN_MEANINGFUL, MAX] to 0-100.
#
# The floor is not cosmetic. Measured over 397 stored snapshots, the median
# star rating of a top 10 ran p05=4.57 to p95=4.84 — sd 0.21 on a 0-5 ruler.
# Divided by 5 that became 91.4 to 96.9, a component with almost no variance,
# and since `comp_incumbent` multiplies review mass BY it, the interaction
# collapsed to `sqrt(95) * sqrt(rating_mass)` — a constant rescale of review
# mass wearing the costume of an interaction.
#
# 4.0 is where the real distribution starts: a top 10 whose median app sits at
# 4.0 stars is about as soft as the App Store gets, and calling that 0 spends
# the scale on the range that actually occurs. Below 4.0 clamps to 0, which is
# correct — "even weaker than the weakest real SERP" is still just weak.
COMP_MAX_STARS = 5.0
COMP_MIN_MEANINGFUL_STARS = 4.0

# How many SERP results feed the competition score.
COMPETITION_TOP_N = 10

# comp_app_power: what a top-10 app that is *charted at all* is worth, before
# its depth in the chart is graded on top.
#
#   charted -> FLOOR + (1 - FLOOR) * (1 - log(rank) / log(CHART_LIMIT + 1))
#   absent  -> 0
#
# The floor exists because the boundary is a cliff, not a slope. Being in a
# category's top 100 at all is a club with a hard edge; the distance between
# rank 99 and "not on the chart" is far larger than between rank 99 and rank
# 50. A pure log-depth curve says the opposite, and measured over 49 live SERPs
# it collapsed the component onto its floor (mean 11.3, median 3.8) because
# most top-10 slots are uncharted and the charted ones mostly sit deep.
#
# At 0.35 the component spreads properly across the range: mean 18.9, sd 20.5,
# quartiles 5.8 / 9.4 / 27.1, max 88.9 over the same sample.
COMP_APP_POWER_CHARTED_FLOOR = 0.35

# comp_app_power rank decay, same shape and reasoning as
# COMP_PUBLISHER_RANK_DECAY: a giant sitting at rank 1 is a bigger obstacle
# than the same giant at rank 10.
COMP_APP_POWER_RANK_DECAY = 0.15

# comp_publisher: how many slots in the *full* result set a publisher must hold
# before its presence in the top 10 counts as full concentration.
#
# `min(log(slots) / log(SATURATION), 1)` per top-10 slot. One slot scores 0 (a
# publisher with a single app is not holding the space), 2 slots scores half,
# 4 or more scores 1.
#
# This replaced a binary `slots >= 2` threshold, which gave a publisher with two
# apps exactly the credit of one with twenty.
#
# WHY NOT A HERFINDAHL INDEX
# --------------------------
# The obvious textbook answer is the normalized HHI over the whole field, and
# it was measured before being rejected. Over 24 live 200-result SERPs it
# returned mean 0.4, sd 1.0, max 5.1 — on a 0-100 ruler. A ~190-app field is
# inherently diffuse (150-190 distinct sellers is typical), so the normalized
# index sits against its floor for every keyword in the store and discriminates
# nothing. Concentration in app search is not a property of the whole field; it
# is the question of whether the publishers holding the *top* slots also hold
# others, which is what this component measures.
#
# 4.0 chosen over 2.0 and 8.0 on the same 24 SERPs. 2.0 saturates at the old
# binary threshold and is therefore the old component wearing a logarithm; 8.0
# grades well but costs a third of the component's level (mean 13.9 vs 25.0).
# 4.0 grades genuinely while landing near the old level (mean 19.5, sd 20.0 vs
# the old 25.0/22.2), so it does not fight the fitted weight.
#
# A candidate for fitting rather than choosing, once the calibration sample
# covers more than four topic clusters.
COMP_PUBLISHER_SATURATION_SLOTS = 4.0

# comp_publisher rank decay: slot i of the top 10 is weighted 1 / (1 + DECAY*i).
#
# At 0.15 the first slot counts ~2.4x the tenth. Position matters because the
# component asks how hard the space is to break into, and a publisher holding
# ranks 1-3 is a materially higher wall than one holding 8-10. Every other
# component still takes a flat median over the top 10; this is the first place
# rank enters the model at all.
#
# 0.0 recovers the flat average.
COMP_PUBLISHER_RANK_DECAY = 0.15

# comp_exact_match: an app matching every keyword token, but scattered rather
# than as a contiguous phrase, scores this fraction of a true phrase match.
#
# Apple does not require a contiguous title phrase to rank an app, so "Daylio -
# Journal & Diary" is a real competitor for "smart journal" even though it
# contains neither the phrase nor both words. Scoring it 0 was the single
# largest source of downward bias in the model: 194 of 397 stored snapshots had
# comp_exact_match at exactly 0 while it carried a quarter of the weight.
COMP_EXACT_MATCH_PARTIAL_CEILING = 0.75


# Exponent for the weighted power mean in `competition.combine`.
#
# 1.0 is the plain arithmetic mean. Above 1.0 the high components pull harder
# than the low ones, and 2.0 (the quadratic mean) is what this model uses.
#
# WHY NOT A PLAIN AVERAGE
# -----------------------
# Ranking for a keyword means clearing every barrier it puts up, not the
# average barrier. A term whose top 10 is held by giants but whose titles say
# nothing about the query is *hard* — you still have to displace the giants —
# yet an arithmetic mean reports it as middling because one component is low.
# Difficulty aggregates closer to the maximum than to the mean, and the power
# mean interpolates between the two.
#
# FITTED alongside the weights, over the grid (1.0, 1.5, 2.0, 3.0). It was
# briefly a hand-set dial justified only by argument. It no longer is: 2.0 is
# what `aso calibrate-competition` selected against AppFigures. Turn it down to
# 1.0 to recover the plain mean; the model stays coherent at any value >= 1.0.
#
# It was 3.0 before `comp_app_power` existed, and the drop is informative. A
# steep exponent was doing by brute force what a missing component should have
# been doing by measurement: with nothing in the model that spiked on hard
# keywords, the only way to stop them being averaged down was to lean hard on
# whatever component happened to be highest. Given a term that actually
# measures incumbent size, the model needs less of that crutch.
COMPETITION_AGGREGATION_POWER = 2.0


# ---------------------------------------------------------------------------
# Search-volume (prefix ladder) constants
# ---------------------------------------------------------------------------
# The search score is a PROXY, not measured volume. It asks: how early in the
# typing of this keyword does the App Store start suggesting it, and how high
# does it sit in that suggestion list? Terms Apple surfaces from two characters
# in are terms lots of people type. See scoring/search.py for the mapping, for
# the SATURATION note on what this proxy provably cannot tell you, and for
# `calibrate()`, the hook that will later fit these constants against real
# Apple Search Ads impression counts.

# Shortest prefix we're willing to query. 1-char prefixes are noisy, but they
# are also the only rung that separates a genuinely enormous term from a merely
# distinctive one: at 2 characters far too many keywords sit at rank 1 and the
# score saturates. Costs one extra request per keyword. See the SATURATION note
# in scoring/search.py.
SEARCH_MIN_PREFIX_LEN = 1

# Hard cap on autocomplete requests per keyword, to bound a refresh run.
# Longer keywords get their prefix ladder sampled down to this many rungs.
SEARCH_MAX_PREFIX_QUERIES = 12

# How the observations combine into one 0-100 score. Must sum to 1.
#
# FITTED, not chosen. Derived 2026-08-09 against 180 keywords with AppFigures
# popularity spanning 5-97, selected by 5-fold cross-validation over a joint
# grid of decay, cap and weights. Re-derive with `aso calibrate`; do not
# hand-edit and expect the numbers below to still describe reality.
#
#   depth + rank only (the previous model)   CV rank correlation 0.487
#   depth + extensions + rating mass         CV rank correlation 0.782
#
# WHY RATING MASS IS NO LONGER A DEMAND SIGNAL
# --------------------------------------------
# It was, and the evidence for it was good: measured against 180 keywords with
# AppFigures popularity, the median review count of the top 10 scored 0.660 on
# its own — better than the entire autocomplete ladder (0.478) — and it kept
# working for the 52% of keywords autocomplete never surfaces at all, where it
# still scored 0.608. That is why it held weight 0.70 here.
#
# It was moved to the competition side (see COMPETITION_WEIGHTS, where it now
# feeds `comp_incumbent`) because the premise changed. Review mass was only
# ever a *proxy* for demand — a way of inferring how valuable a query is from
# who won it. Apple's popularity index measures the same quantity directly, so
# the proxy's job on this side is done, and review mass goes back to describing
# what it describes literally: how entrenched the incumbents are.
#
# THE COST, STATED PLAINLY
# ------------------------
# This weakens the proxy wherever the proxy is still what runs — keywords Apple
# censors below 5, and every run with `apple_popularity_enabled` false. The
# recorded fit is CV 0.782 with rating mass and 0.487 on depth + rank alone;
# depth + extensions lands between them. That degradation is accepted, not
# overlooked. The two surviving weights keep their fitted 2:1 ratio rather than
# being re-fitted, because re-fitting is `aso calibrate`'s job and inventing
# numbers here would misrepresent them as measured.
#
# The rank and savings weights are 0.0 because the fit put them there. Rank
# correlates 0.108 with demand among surfaced keywords and drops out of every
# blend; characters-saved measures -0.030 and is noise. Both components are
# still computed and stored so a future sample can revive them.
SEARCH_DEPTH_WEIGHT = 0.67  # absolute shortness of the matching prefix
SEARCH_EXTENSIONS_WEIGHT = 0.33  # how many suggestions the keyword is a stem of
SEARCH_RATING_MASS_WEIGHT = 0.00  # review mass — moved to comp_incumbent
SEARCH_SAVINGS_WEIGHT = 0.00  # characters the suggestion saved the typist
SEARCH_RANK_WEIGHT = 0.00  # where it sits in that suggestion list

# Suggestions extending the keyword that saturate the extensions component.
# Apple returns at most 10; the fit chose 6, i.e. "six completions is already
# a thoroughly demanded stem". This is the component that rescues terms the
# ladder cannot see: `insta` (AppFigures 75) is never suggested for any prefix
# of itself, because Apple offers completions rather than echoing the query —
# but ten of its ten suggestions extend it.
SEARCH_EXTENSIONS_CAP = 6

# Depth component decays geometrically per character typed, in ABSOLUTE terms:
# depth 1 -> 100, 2 -> 85, 3 -> 72, 4 -> 61, 5 -> 52. Deliberately not scaled by
# keyword length; scaling by length is what made short keywords unbeatable.
#
# Much gentler than the 0.55 fitted previously. That steepness was the ladder
# compensating for being the only demand signal in the model: it had to spread
# 231 keywords across the handful of values `prefix_depth` can take. With
# rating mass carrying the level, depth only has to nudge, and the fit relaxed
# the decay accordingly.
SEARCH_DEPTH_DECAY = 0.85

# Characters saved (keyword_length - prefix_depth) that saturate the savings
# component. A long phrase Apple completes from a short stem is a real signal,
# but a bounded one — hence a cap rather than a ratio. Currently inert: see
# SEARCH_SAVINGS_WEIGHT above, which the fit drove to zero.
SEARCH_SAVINGS_CAP = 10

# Rank component decays geometrically: rank 1 -> 100, rank 2 -> 50, rank 3 -> 25...
# Weakly identified — it and SEARCH_RANK_WEIGHT both scale how much rank matters,
# so the fit trades one against the other. See CALIBRATION_RANK_DECAY_GRID.
SEARCH_RANK_DECAY = 0.50

# Score for a keyword that never appears in its own autocomplete ladder, even
# at the full string. Not zero — it means "below the measurable floor", and a
# real zero would make opportunity ranking collapse.
SEARCH_NO_MATCH_SCORE = 1.0

# Autocomplete suggestion lists are ~10 long; ranks past this are floor-value.
SEARCH_MAX_HINT_RANK = 10


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
# Three public Apple endpoints, plus — since the popularity work — one
# undocumented one, quarantined below and off by default.
#
# The rule this file used to state was "no undocumented endpoints". That was a
# real constraint honestly held, and it was given up deliberately, not eroded:
# Apple publishes a 5-100 keyword popularity index that every ASO vendor
# resells, the documented route to it needs live ad spend, and inferring a
# number Apple will simply tell you is a strange thing to spend a codebase on.
# What the old rule bought was reliability, so that is what
# APPLE_POPULARITY_ENABLED and the proxy fallback are for.
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
HINTS_URL = "https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints"

# Top-charts RSS. The *legacy* iTunes feed, deliberately, not the newer
# rss.marketingtools.apple.com/api/v2 one.
#
# The v2 feed was tried first and cannot do this job. Measured against it
# directly: `limit` above 100 returns HTTP 500, there is no app grossing chart
# (404), and it has no genre parameter at all. Overall charts alone cover 8.2%
# of the top-10 apps in a sample of 49 live SERPs — the store's 100 biggest
# apps simply are not who you compete with on a mid-tail keyword.
#
# The legacy feed still serves genre charts, which is where all the signal is:
# per-genre free charts cover 30.4% of the same top-10 apps, per-genre grossing
# 32.4%, and the union 40.2%. Both are pulled for that reason.
#
# Undocumented and unversioned, so treat a shape change as expected rather than
# exceptional — `charts.parse_feed` returns an empty index rather than raising,
# and a missing index makes `comp_app_power` `None` rather than 0.
CHARTS_URL_TEMPLATE = (
    "https://itunes.apple.com/{country}/rss/{feed}/limit={limit}/genre={genre}/json"
)

# Both feeds are pulled per genre. Free is the reach proxy, grossing the
# revenue one; an app can be huge on either axis and they overlap only
# partially (index sizes 2365 and 2345, union 3867).
CHART_FEEDS: tuple[str, ...] = ("topfreeapplications", "topgrossingapplications")

# The feed silently caps at 100 however large a `limit` you ask for — 200 comes
# back as 100 rather than as an error. Stated explicitly so nobody re-derives
# it, and so `comp_app_power`'s normalization has a real depth to divide by.
CHART_LIMIT = 100

# App Store genre ids for the charts worth pulling. Games (6014) is one id here
# though the store subdivides it further; the subgenres are not needed, because
# this index answers "is this app big" rather than "where exactly does it sit".
#
# 24 genres x 2 feeds = 48 requests per country, behind a 1-day TTL and shared
# across every keyword in a refresh. Genres with no chart (Catalogs, 6022)
# return empty and are simply absent.
CHART_GENRES: dict[int, str] = {
    6000: "Business",
    6002: "Utilities",
    6003: "Weather",
    6004: "Sports",
    6005: "Social Networking",
    6006: "Medical",
    6007: "Productivity",
    6008: "Photo & Video",
    6009: "News",
    6010: "Navigation",
    6011: "Music",
    6012: "Lifestyle",
    6013: "Health & Fitness",
    6014: "Games",
    6015: "Finance",
    6016: "Travel",
    6017: "Education",
    6018: "Books",
    6020: "Entertainment",
    6021: "Reference",
    6023: "Food & Drink",
    6024: "Shopping",
    6026: "Developer Tools",
    6027: "Graphics & Design",
}

# How long a chart index stays fresh. The feeds update daily, so anything
# shorter re-fetches 48 URLs for the same numbers.
CHARTS_TTL_DAYS = 1
ASA_TOKEN_URL = "https://appleid.apple.com/auth/oauth2/token"
ASA_API_BASE = "https://api.searchads.apple.com/api/v5"

USER_AGENT = "aso-tool/0.1 (+local research tool)"

# How many results to ask the iTunes search endpoint for. 200 is the API's
# documented maximum.
#
# It was 50, and that was not a neutral choice: `resultCount` counts what was
# returned, so the request limit *is* the ceiling on `comp_breadth`. At 50 the
# component's p25-to-p95 spanned 71.9 to 74.2 — a constant wearing the costume
# of a measurement — and the calibration that zeroed its weight was measuring
# that constant, not breadth. Raising the limit is what makes the component
# testable at all.
#
# It also widens the denominator `comp_publisher` counts seller repeats over,
# from 50 slots to 200. Concentration is a property of the field, and reading
# it off the first quarter of the field underestimates it.
#
# Costs no extra request — one call either way, just a larger body — so the
# ~15/min pacing in `http.py` is unaffected. The cache key includes the limit
# (`cache.serp_key`), so changing this invalidates stored SERPs by
# construction rather than silently mixing two widths in one table.
ITUNES_SEARCH_LIMIT = 200

# Token-bucket burst size. 1 means strict pacing — one request every
# 60/rate_limit_per_min seconds, no bursting. Raising this lets a run fire N
# requests back to back before throttling, which is exactly how you trip
# Apple's 403 threshold at the start of a long refresh. Raise with care.
RATE_LIMIT_BURST = 1

# Backoff between retries: exponential from INITIAL, capped at MAX, with up to
# JITTER seconds of randomness so parallel workers don't retry in lockstep.
RETRY_INITIAL_WAIT_SECONDS = 2.0
RETRY_MAX_WAIT_SECONDS = 60.0
RETRY_JITTER_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Apple Search Ads
# ---------------------------------------------------------------------------
# ASA is an authenticated API on a different host with its own quota, so it gets
# its own Fetcher and its own rate limit. Sharing the 15/min iTunes bucket would
# throttle it for no reason — that number exists to stay under a public
# endpoint's unpublished 403 threshold, which has nothing to do with ASA.
ASA_RATE_LIMIT_PER_MIN = 60

# The client-assertion JWT's lifetime. Apple permits up to 180 days; a short one
# is better because the assertion is minted fresh per token exchange anyway and
# a leaked long-lived assertion is a standing credential.
ASA_JWT_LIFETIME_SECONDS = 900

# Refresh the access token this long before it actually expires, so a report
# that starts near the boundary doesn't 401 halfway through.
ASA_TOKEN_REFRESH_MARGIN_SECONDS = 60

# OAuth scope and assertion type, both fixed by Apple.
ASA_SCOPE = "searchadsorg"
ASA_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASA_JWT_AUDIENCE = "https://appleid.apple.com"

# Rows per page when pulling a search-terms report. Apple's maximum is 1000.
ASA_REPORT_PAGE_SIZE = 1000

# Safety valve: stop paging a single report after this many rows.
ASA_REPORT_MAX_ROWS = 50_000

# Default lookback for `aso asa pull`. 90 days is long enough for low-volume
# terms to accumulate a usable impression count without reaching so far back
# that the store's ranking has meaningfully changed underneath it.
ASA_DEFAULT_LOOKBACK_DAYS = 90


# ---------------------------------------------------------------------------
# Apple keyword popularity (undocumented)
# ---------------------------------------------------------------------------
# Apple's own demand index, the number AppFigures' Popularity is derived from.
# The documented route to it is the Impression Share report, which only covers
# search terms your ads actually competed on — useless without spend. This is
# the other route: the endpoint behind the keyword tool in the Search Ads web
# dashboard, which answers for any keyword.
#
# VERIFIED against a live capture of the Apple Ads dashboard, 2026-08-09.
#
# It is NOT on api.searchads.apple.com and it is NOT a REST endpoint, which is
# what an earlier draft of this assumed. It is a GraphQL operation on the web
# dashboard's own host, authenticated by the browser SESSION COOKIE — the ASA
# JWT bearer token has no standing here at all.
#
# The operation takes a seed `text` and returns up to ~100 related keywords,
# each with Apple's 5-100 popularity. The seed's own popularity is in that list
# when Apple scores it, and absent when it does not — which is precisely the
# censoring signal `scoring.blend` consumes. Confirmed live: instagram 99,
# insta 73, habit tracker 52, habit 47, and `finsta` absent from its own
# results.
APPLE_POPULARITY_URL = "https://app-ads.apple.com/reporting/graphql"
APPLE_POPULARITY_OPERATION = "getRecommendedKeywordsGql"
APPLE_POPULARITY_QUERY = """
query getRecommendedKeywordsGql($adamId: String!, $text: String, $storefronts: [String]) {
  recommendationV2 {
    getRecommendedKeywords(adamId: $adamId, text: $text, storefronts: $storefronts) {
      name
      popularity
    }
  }
}
""".strip()

# One request per keyword: the operation takes a single seed, not a list.
APPLE_POPULARITY_BATCH_SIZE = 1

# How the session is held. "browser" keeps a real profile on disk and is the
# only option that survives a cookie expiring without a human pasting a new
# one; "cookie" reads ASO_APPLE_COOKIE and needs no extra dependency; "auto"
# prefers the browser profile when one exists and falls back to the cookie.
APPLE_TRANSPORT_BROWSER = "browser"
APPLE_TRANSPORT_COOKIE = "cookie"
APPLE_TRANSPORT_AUTO = "auto"
APPLE_TRANSPORTS = (APPLE_TRANSPORT_AUTO, APPLE_TRANSPORT_BROWSER, APPLE_TRANSPORT_COOKIE)

# Pace against the dashboard's own host. This is a UI backend, not a bulk API,
# and the polite reading of that is roughly what a fast human clicking around
# would produce.
APPLE_POPULARITY_RATE_PER_MIN = 240

# Apple reports 5-100 and reports nothing below 5. The floor is therefore also
# the ceiling on every censored term: "below the least popular thing I will
# name". See db.py migration 6 and `scoring.blend`.
APPLE_POPULARITY_FLOOR = 5.0
APPLE_POPULARITY_CEILING = 100.0

# How long a popularity reading stays fresh. Apple's index lags impressions by
# roughly four days and moves slowly, so a short TTL buys nothing but requests
# against an undocumented endpoint — which is the one budget worth conserving.
APPLE_POPULARITY_TTL_DAYS = 14

# Which demand source wins when a keyword has several.
#
# NOT a union, and the order is not cosmetic. AppFigures' popularity is itself
# derived from Apple's indicator, so pooling the two would let one underlying
# fact vote twice and read as independent corroboration. `asa` sits between
# them because measured impressions are ground truth where they exist, but they
# exist only for terms your ads won.
DEMAND_SOURCE_PRIORITY: tuple[str, ...] = ("apple", "asa", "appfigures")

# Below this many keywords carrying BOTH a proxy score and an Apple value, the
# bridge is a step function drawn through anecdotes. See scoring/bridge.py.
BRIDGE_MIN_OVERLAP = 12

# The range a bridged competition score is clamped to.
#
# 0, not APPLE_POPULARITY_FLOOR. Difficulty and popularity are different scales
# that happen to share a formatting: Apple censors popularity below 5 so no
# keyword can honestly be reported under it, whereas a keyword genuinely
# nobody contests is a 0. Clamping difficulty up to 5 would invent competition.
COMPETITION_BRIDGE_FLOOR = 0.0
COMPETITION_BRIDGE_CEILING = 100.0

# The competition bridge needs more overlap than the demand one.
#
# The demand bridge maps onto Apple's own published index, where the target is
# a measurement. The competition bridge maps onto a *vendor's derived* index,
# which carries its own model error, so a step function fitted through a dozen
# points is fitting that vendor's noise as much as our own bias. 40 matches
# CALIBRATION_MIN_SAMPLES for the same reason.
COMPETITION_BRIDGE_MIN_OVERLAP = 40

# Which vendor's difficulty scale the competition bridge maps onto.
#
# Unlike DEMAND_SOURCE_PRIORITY this is a single name, not a fallback chain,
# because a bridge anchors the whole column to one ruler. Mixing two vendors'
# levels in one column is the exact problem the bridge exists to fix.
COMPETITION_BRIDGE_SOURCE = "appfigures"


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
# Refuse to fit below this many usable samples. Six free parameters fitted to a
# dozen points is not calibration, it is decoration — and a confidently wrong
# scale is worse than an honestly uncalibrated one.
CALIBRATION_MIN_SAMPLES = 20

# A target needs *spread*, not just rows. Vendors report a floor value for
# anything below their measurable threshold (AppFigures uses 5), and a sample
# drawn from one long-tail cluster can be almost entirely floor: 21 of 26 in
# the first real run. Fitting an ordering that barely exists produces a
# confident number backed by nothing, so require this many distinct target
# values and cap how much of the sample any single value may occupy.
CALIBRATION_MIN_DISTINCT_TARGETS = 6
# Fraction of keyword PAIRS that must be orderable (i.e. not tied on the
# target) before a fit is allowed. Spearman is a statistic over pairs, so this
# is the direct measure of "is there an ordering here to fit".
#
# It replaced a modal-share gate at 0.5, which was a proxy for the same thing
# and a bad one. The sample that motivated the guard originally — one long-tail
# cluster, ~81% on the vendor's floor — leaves roughly 34% of pairs orderable
# and is still rejected. A sample where half the keywords sit on the floor and
# the other half span 5-97 leaves 74% orderable and is now correctly accepted,
# where modal share rejected it at 51%.
CALIBRATION_MIN_ORDERABLE_PAIRS = 0.5

# Grids the fit searches. Coarse on purpose: the data is noisy enough that more
# precision would be false precision, and a readable exhaustive search beats an
# opaque optimizer for a once-a-quarter job.
#
# 0.05, not the 0.025 this used to be. The finer grid was measured against the
# coarser one on 66 real keywords: it moved the best rank correlation from
# 0.5135 to 0.5137 and picked the same weights, while costing 2.8x the runtime
# — and the search runs six times per calibration (once, plus five CV folds),
# so that pushed the test suite past ten minutes. Going coarser still, to 0.1,
# does lose real quality (0.503), so this is the stopping point rather than an
# arbitrary round number.
CALIBRATION_DEPTH_DECAY_GRID = tuple(round(0.50 + 0.05 * i, 3) for i in range(10))

# Runs to 1.00, not 0.95. A decay of exactly 1.0 makes the rank term constant
# across every rank — the hypothesis that *where* a keyword sits in a
# suggestion list says nothing about demand, only that it appears at all.
# That is not a hypothetical: measured against AppFigures popularity over 66
# keywords, hint_rank correlates at +0.097 while prefix_depth reaches +0.478.
# With the grid stopping at 0.95 the fit pinned to the boundary, which is the
# search reporting "I want to go further" rather than an optimum.
#
# The low end stays at 0.50 even though the fit pins there too, and that is a
# deliberate refusal rather than an oversight. Extending it downward was
# measured: 0.50 -> 0.30 -> 0.10 moved rank correlation 0.5135 -> 0.5205 ->
# 0.5264, gains in the third decimal on 66 samples whose CV folds span +-0.3.
# The cause is weak identifiability — this constant and SEARCH_RANK_WEIGHT both
# scale how much the rank term matters, so the fit trades one against the other
# and neither is pinned down. Expect a standing boundary warning here; treat it
# like the SEARCH_SAVINGS_CAP one, as a property of the parameterization rather
# than a truncated search. Chasing it costs 3x runtime for noise.
#
# COLLAPSED TO ONE VALUE, for the same reason as CALIBRATION_SAVINGS_CAP_GRID:
# the fit now drives SEARCH_RANK_WEIGHT to 0.0, which makes this constant
# unidentifiable rather than merely weakly identified. Rank correlates 0.108
# with real demand among the keywords autocomplete surfaces at all, and drops
# out of every blend that includes rating mass. Restore
# `tuple(round(0.50 + 0.05 * i, 3) for i in range(11))` if it ever earns a
# weight back.
CALIBRATION_RANK_DECAY_GRID = (0.50,)

# Resolution of the weight simplex the fit searches. The three weights are
# chosen by rank correlation rather than by the closed-form least-squares
# solve, because the target is ordinal and Spearman is what the fit is graded
# on; see `_best_weights`. 0.1 gives 66 triples per grid point. Halving it to
# 0.05 quadruples the cost and returned an identical answer on real data, and
# finer resolution than this would be false precision from a few dozen noisy
# samples anyway.
CALIBRATION_WEIGHT_STEP = 0.1


# The fit drives SEARCH_SAVINGS_WEIGHT to 0.0 on real data, which makes this
# cap unidentifiable — every value scores identically once its weight is zero,
# so the reported "best" is just the first entry.
#
# COLLAPSED TO ONE VALUE. It used to be (10, 15, 20, 25, 30). Adding two
# components took the weight simplex from 66 tuples per grid point to 1001, so
# the decay grids had to give the cost back somewhere, and searching five
# values of a constant that provably cannot affect the result was the obvious
# place. Restore the full tuple if a future sample ever gives the savings
# component a non-zero weight — until then it would be 5x the runtime to
# rediscover a tie.
CALIBRATION_SAVINGS_CAP_GRID = (10,)

# Suggestions-extending-the-keyword that saturate the extensions component.
# Apple returns at most 10, so values above that are indistinguishable.
CALIBRATION_EXTENSIONS_CAP_GRID = (4, 6, 8, 10)


def _env_str(key: str, default: str) -> str:
    value = os.getenv(key)
    return value if value not in (None, "") else default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


def _env_path(key: str) -> Path | None:
    raw = os.getenv(key)
    return Path(raw).expanduser() if raw else None


def _env_choice(key: str, default: str, allowed: tuple[str, ...]) -> str:
    """Parse an enum-valued setting, refusing anything not in `allowed`.

    A typo here would silently pick the default transport, and the two
    transports fail in completely different ways — debugging a cookie error
    while believing you are on the browser path is a bad afternoon.
    """
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    value = raw.strip().lower()
    if value not in allowed:
        raise ValueError(f"{key} must be one of {', '.join(allowed)}, got {raw!r}")
    return value


def _env_bool(key: str, default: bool) -> bool:
    """Parse a boolean, refusing anything ambiguous.

    A typo'd flag must not silently mean False. This one gates calls to an
    undocumented endpoint, so "I set it to `yes` and nothing happened" is a
    failure mode worth an exception rather than a shrug.
    """
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(
        f"{key} must be a boolean (true/false, 1/0, yes/no, on/off), got {raw!r}"
    )


@dataclass(frozen=True)
class ASASettings:
    """Apple Search Ads credentials. See clients/asa.py.

    Every field comes from the Search Ads UI except `private_key_path`, which
    points at a P-256 private key you generate yourself and whose public half
    you paste into Account Settings → API. Apple never sees the private key and
    cannot re-issue it; losing it means registering a new API user.
    """

    client_id: str | None = None
    team_id: str | None = None
    key_id: str | None = None
    org_id: str | None = None
    private_key_path: Path | None = None

    @property
    def configured(self) -> bool:
        return not self.missing

    @property
    def missing(self) -> list[str]:
        """Which env vars are unset, for an error message worth reading."""
        pairs = {
            "ASO_ASA_CLIENT_ID": self.client_id,
            "ASO_ASA_TEAM_ID": self.team_id,
            "ASO_ASA_KEY_ID": self.key_id,
            "ASO_ASA_ORG_ID": self.org_id,
            "ASO_ASA_PRIVATE_KEY_PATH": self.private_key_path,
        }
        return [name for name, value in pairs.items() if not value]


@dataclass(frozen=True)
class Settings:
    db_path: Path = PROJECT_ROOT / "aso.db"
    default_country: str = "us"

    # Rate limiting, shared across every iTunes + hints call in a process.
    rate_limit_per_min: int = 15
    max_concurrency: int = 3
    http_timeout_seconds: float = 20.0
    retry_attempts: int = 4

    # Cache TTLs in days.
    serp_ttl_days: int = 3
    app_ttl_days: int = 7
    hints_ttl_days: int = 3
    apple_popularity_ttl_days: int = APPLE_POPULARITY_TTL_DAYS

    # Gates every call to the undocumented popularity endpoint. Off by default:
    # an unverified endpoint should be opted into, and with it off the demand
    # score is exactly what it was before this feature existed.
    apple_popularity_enabled: bool = False

    # The dashboard session cookie, copied from a signed-in browser, and the
    # app whose recommendations are being asked for.
    #
    # A cookie is a poor credential to keep in a file and this one expires on
    # its own schedule — but it is what the endpoint accepts, and the honest
    # move is to say so rather than to pretend a bearer token works. `aso apple
    # pull` reports a stale cookie as a stale cookie.
    apple_cookie: str | None = None
    apple_adam_id: str | None = None

    # The browser profile that holds the signed-in session. This directory is a
    # credential — it is gitignored alongside the ASA private key.
    apple_profile_dir: Path = PROJECT_ROOT / ".apple-session"
    apple_transport: str = APPLE_TRANSPORT_AUTO

    @property
    def apple_profile_exists(self) -> bool:
        return self.apple_profile_dir.is_dir() and any(
            self.apple_profile_dir.iterdir()
        )

    competition_weights: dict[str, float] = field(
        default_factory=lambda: dict(COMPETITION_WEIGHTS)
    )
    asa: ASASettings = field(default_factory=ASASettings)

    @classmethod
    def from_env(cls) -> "Settings":
        db_path = _env_path("ASO_DB_PATH") or (PROJECT_ROOT / "aso.db")
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        return cls(
            db_path=db_path,
            default_country=_env_str("ASO_DEFAULT_COUNTRY", "us").lower(),
            rate_limit_per_min=_env_int("ASO_RATE_LIMIT_PER_MIN", 15),
            max_concurrency=_env_int("ASO_MAX_CONCURRENCY", 3),
            http_timeout_seconds=_env_float("ASO_HTTP_TIMEOUT_SECONDS", 20.0),
            retry_attempts=_env_int("ASO_RETRY_ATTEMPTS", 4),
            serp_ttl_days=_env_int("ASO_SERP_TTL_DAYS", 3),
            app_ttl_days=_env_int("ASO_APP_TTL_DAYS", 7),
            hints_ttl_days=_env_int("ASO_HINTS_TTL_DAYS", 3),
            apple_popularity_ttl_days=_env_int(
                "ASO_APPLE_POPULARITY_TTL_DAYS", APPLE_POPULARITY_TTL_DAYS
            ),
            apple_popularity_enabled=_env_bool("ASO_APPLE_POPULARITY_ENABLED", False),
            apple_cookie=os.getenv("ASO_APPLE_COOKIE") or None,
            apple_adam_id=os.getenv("ASO_APPLE_ADAM_ID") or None,
            apple_profile_dir=(
                _env_path("ASO_APPLE_PROFILE_DIR") or PROJECT_ROOT / ".apple-session"
            ),
            apple_transport=_env_choice(
                "ASO_APPLE_TRANSPORT", APPLE_TRANSPORT_AUTO, APPLE_TRANSPORTS
            ),
            competition_weights=dict(COMPETITION_WEIGHTS),
            asa=ASASettings(
                client_id=os.getenv("ASO_ASA_CLIENT_ID") or None,
                team_id=os.getenv("ASO_ASA_TEAM_ID") or None,
                key_id=os.getenv("ASO_ASA_KEY_ID") or None,
                org_id=os.getenv("ASO_ASA_ORG_ID") or None,
                private_key_path=_env_path("ASO_ASA_PRIVATE_KEY_PATH"),
            ),
        )


settings = Settings.from_env()


def reload() -> Settings:
    """Re-read .env and the environment into the module-level `settings`."""
    global settings
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    settings = Settings.from_env()
    return settings
