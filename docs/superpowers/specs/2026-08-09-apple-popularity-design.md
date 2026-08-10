# Apple keyword popularity: teacher and answer

Date: 2026-08-09
Status: implemented, endpoint unverified

## Problem

The demand score is a proxy. It reaches a cross-validated Spearman of 0.718
against AppFigures popularity, and the README is explicit that this proves
less than it looks: AppFigures' popularity is *itself derived from* Apple's
keyword popularity indicator, so part of that correlation is agreement with
another estimator rather than with reality.

Apple publishes the real number — a 5–100 index — and every ASO vendor resells
it. There are three routes to it:

1. **Impression Share reports** (documented, Campaign Management API v5).
   Carries `searchPopularity` per search term and country. Scoped to terms the
   advertiser competed on, so it needs live ad spend. `asa_search_terms` is
   empty here; this route is closed.
2. **The Search Ads dashboard keyword tool** (undocumented). Answers for any
   keyword, no spend. This is what the vendors resell.
3. **AppFigures**, already imported — Apple's number at one remove.

## Decision

Take route 2, and treat the number as **both teacher and answer**:

- **Teacher** — it becomes the calibration target, displacing AppFigures.
- **Answer** — where Apple has a value, that value *is* the demand score; the
  proxy covers only what Apple will not serve.

Rejected: teacher-only (leaves a measured number unused for the keywords it
covers) and answer-only (a column mixing Apple's scale with the proxy's is not
sortable, and `opportunity` multiplies by it).

This gives up the project's "no undocumented endpoints" stance. That stance
bought reliability, so reliability is bought back explicitly: an opt-in flag, a
proxy fallback on every failure path, and the endpoint's shape confined to two
files.

## Design

| Piece | Where | Depends on endpoint |
|---|---|---|
| 1. Popularity client | `clients/apple_popularity.py` | yes |
| 2. Storage | `demand_observations`, `source='apple'` | no |
| 3. Source priority | `config.DEMAND_SOURCE_PRIORITY` | no |
| 4. Scale bridge | `scoring/bridge.py` | no |
| 5. Blend + provenance | `scoring/blend.py`, `snapshots.search_source` | no |
| 6. Kill switch | `ASO_APPLE_POPULARITY_ENABLED` | no |
| 7. Evaluation | `aso eval` | no |

### Storage

`demand_observations` already carried `source` and `scale`, and Apple's index
is `ordinal_100` — the same scale AppFigures uses. No new table.

One migration adds `censored`. Apple reports nothing below its threshold, which
is a measurement ("less popular than anything I will name"), not a gap.
Encoding it as a magic `0.0` would repeat the conflation this schema refuses
everywhere else.

### Source priority, not union

`("apple", "asa", "appfigures")`. Both Apple and AppFigures are `ordinal_100`,
so `calibrate()` would happily pool them — letting one underlying fact vote
twice and read as independent corroboration.

### The bridge

The proxy's *ordering* is evidence; its absolute level is arbitrary. Mixing a
raw proxy score with an Apple value in one sorted column compares two rulers.

Fitted by pool-adjacent-violators over the keywords carrying both. Monotone by
construction, so it cannot reorder — the ordering is the part that was
measured. Clamped rather than extrapolated outside the observed range.

**The stored quality figure is RMSE, deliberately not Spearman.** A monotone
map cannot change a rank correlation at all, so a before/after Spearman would
print the same number twice and look like evidence.

### Blend

| Case | Score | `search_source` |
|---|---|---|
| Apple scored it | Apple's value | `apple` |
| Apple declined (censored) | `min(bridge(proxy), 5.0)` | `proxy_censored` |
| Never asked / failed / flag off | `bridge(proxy)` | `proxy` |

The censored cap matters: it is precisely the population where the proxy's
documented failure lives (`finsta` measures 5, the proxy says 92), and Apple
has just bounded it.

`snapshots.search_score_proxy` keeps the unblended score. Without it the blend
eats its own input — `search_score` becomes Apple's value for exactly the
keywords that anchor the bridge, the next fit maps Apple onto Apple, returns
the identity and reports a flawless RMSE. It also makes `rescore` idempotent.

## Verification

- With no popularity pulled: 400 snapshots rescored, **0 changed**, every row
  tagged `proxy`, `search_score_proxy == search_score` for all 400.
- `aso eval` reproduces **+0.779** on 182 keywords — the README's independently
  known figure.
- Bridge fitted against AppFigures: 182 overlapping keywords, 27 knots,
  **RMSE 13.03**.

## Open

The endpoint's path, request body and auth are inferred, not captured. Probing
needs either ASA credentials in `.env` or a signed-in browser session on
`app.searchads.apple.com`. Until then the flag stays false and `config.
APPLE_POPULARITY_URL` is a guess, isolated so verifying it is a two-file edit.
