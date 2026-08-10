# Graph Report - ASO-Tool  (2026-08-10)

## Corpus Check
- 72 files · ~106,726 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1885 nodes · 5876 edges · 63 communities detected
- Extraction: 47% EXTRACTED · 53% INFERRED · 0% AMBIGUOUS · INFERRED: 3115 edges (avg confidence: 0.66)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]

## God Nodes (most connected - your core abstractions)
1. `Fetcher` - 179 edges
2. `ChartIndex` - 142 edges
3. `ITunesClient` - 124 edges
4. `HintsClient` - 117 edges
5. `Settings` - 114 edges
6. `ChartsClient` - 103 edges
7. `get()` - 94 edges
8. `FetchError` - 90 edges
9. `session()` - 78 edges
10. `AppRecord` - 77 edges

## Surprising Connections (you probably didn't know these)
- `ASASettings` --uses--> `A real P-256 key, so signing is genuinely exercised rather than mocked.`  [INFERRED]
  aso\config.py → tests\test_asa.py
- `ASASettings` --uses--> `The signature has to be real — a malformed one fails opaquely at Apple.`  [INFERRED]
  aso\config.py → tests\test_asa.py
- `ASASettings` --uses--> `A URL is logged by httpx, by proxies, and by Apple. The body is not.`  [INFERRED]
  aso\config.py → tests\test_asa.py
- `ASASettings` --uses--> `Apple's docs show query params. If the body is refused, try their way.`  [INFERRED]
  aso\config.py → tests\test_asa.py
- `ASASettings` --uses--> `Refreshing early stops a long report 401ing at the boundary.`  [INFERRED]
  aso\config.py → tests\test_asa.py

## Hyperedges (group relationships)
- **Eight competition components combined by fitted power mean** — readme_competition_score, readme_comp_rating_count, readme_comp_exact_match, readme_comp_publisher, readme_comp_app_power, readme_comp_stars, readme_comp_recency, readme_comp_breadth, readme_comp_incumbent, readme_power_mean_rationale [EXTRACTED 1.00]
- **Three independent demand instruments from two Apple endpoints** — readme_demand_score, readme_prefix_depth, readme_search_hint_extensions, readme_comp_rating_count, readme_autocomplete_saturates, readme_serp_inherits_category [EXTRACTED 1.00]
- **Apple popularity: client → storage → priority → bridge → blend, behind a kill switch** — spec_popularity_client, spec_demand_observations, spec_source_priority, spec_bridge, spec_blend, readme_apple_popularity_flag, spec_aso_eval [EXTRACTED 1.00]

## Communities

### Community 0 - "Community 0"
Cohesion: 0.02
Nodes (290): add(), export(), import_competition(), import_demand(), import_keywords(), track(), caveats(), detail_view() (+282 more)

### Community 1 - "Community 1"
Cohesion: 0.02
Nodes (212): _as_int(), ASAClient, build_client_assertion(), _has_more(), _parse_json(), parse_search_term_rows(), CachedBody, charts_key() (+204 more)

### Community 2 - "Community 2"
Cohesion: 0.03
Nodes (218): How the popularity GraphQL call actually gets made.  Two ways to hold an Apple A, Send the request with a pasted `Cookie` header. No browser required., Drive a persistent browser profile, and run the query inside the page.      The, Open a real window and wait for the human to sign in.      Deliberately headed a, The popularity endpoint failed or answered in a shape we do not know., The session is no longer signed in.      Its own class because it is the failure, Ask Apple about one seed keyword, return the parsed JSON body., ASAAuthError (+210 more)

### Community 3 - "Community 3"
Cohesion: 0.03
Nodes (197): True when Apple supplied the number rather than the proxy., Bridge, NotEnoughOverlap, Too few keywords carry both a proxy score and an Apple value.      Fitting a ste, A fitted monotone map from proxy score onto Apple's scale.      `knots` is ascen, ChartIndex, app_power_component(), _best_on() (+189 more)

### Community 4 - "Community 4"
Cohesion: 0.02
Nodes (174): opportunity(), Opportunity score — the default sort everywhere.      opportunity = search_score, Combine the two scores. `None` if either is unknown.      Unknown is not zero: g, _best_weights(), calibrate(), Calibration, CalibrationSample, _components() (+166 more)

### Community 5 - "Community 5"
Cohesion: 0.06
Nodes (74): ApplePopularityClient, ApplePopularityDisabled, ApplePopularityNotConfigured, build_transport(), _coerce_popularity(), normalize_term(), parse_recommendations(), PopularityResult (+66 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (71): BaseModel, get_conn(), FastAPI dependencies., One connection per request, closed when it ends.      `check_same_thread=False, client(), cancel_job(), get_job(), Job (+63 more)

### Community 7 - "Community 7"
Cohesion: 0.05
Nodes (70): create_app(), lifespan(), App factory and lifespan.  The lifespan owns the one `Fetcher` this process ge, apple_login(), apple_pull(), apple_status(), asa_campaigns(), asa_pull() (+62 more)

### Community 8 - "Community 8"
Cohesion: 0.04
Nodes (81): AppFigures keyword export (popularity + competitiveness), aso apple login (visible browser, waits for human), Apple popularity endpoint (app-ads.apple.com/reporting/graphql), ASO_APPLE_POPULARITY_ENABLED kill switch, ASO_APPLE_TRANSPORT: cookie vs browser session transports, Apple Search Ads API v5 client, What ASA data can and cannot support (only terms your ads won), ES256 JWT client assertion auth (assertion in POST body) (+73 more)

### Community 9 - "Community 9"
Cohesion: 0.05
Nodes (68): CompetitionImportResult, Format, ImportError_, ImportResult, Readers for demand data exported from tools that already measure it.  The auto, Read a vendor CSV into `DemandWrite` rows.      `country` is a parameter rathe, Read a vendor's difficulty column out of the same CSV `read_demand_csv` reads., A file we can't read as the claimed format. (+60 more)

### Community 10 - "Community 10"
Cohesion: 0.06
Nodes (63): blend(), BlendedScore, Combine Apple's popularity index with the proxy into one demand column.  THE THR, A demand score and the ruler that produced it., Resolve one keyword's demand score.      Returns None when there is nothing to s, fit(), from_json(), _pava() (+55 more)

### Community 11 - "Community 11"
Cohesion: 0.12
Nodes (57): rescore(), backfill_extensions(), refresh(), rescore(), all_snapshots_for_rescoring(), latest_snapshot(), Store a fitted bridge. Never updates in place — bridges are append-only., Every snapshot with the stored components a re-score needs.      Joins in `key (+49 more)

### Community 12 - "Community 12"
Cohesion: 0.09
Nodes (37): purge_expired(), Drop expired rows of one kind. Returns how many were removed., conn(), A migrated, throwaway database., applied_versions(), connect(), migrate(), parse_ts() (+29 more)

### Community 13 - "Community 13"
Cohesion: 0.11
Nodes (32): blend_outcome(), apple_demand_map(), latest_bridge(), preferred_demand_source(), The newest bridge of one metric for this storefront, or None., (keyword, country) -> (value, censored) for one demand source.      Loaded onc, The highest-priority demand source that actually has uncensored rows.      Cal, measure() (+24 more)

### Community 14 - "Community 14"
Cohesion: 0.1
Nodes (22): _env_bool(), _env_choice(), _env_float(), _env_int(), _env_path(), _env_str(), from_env(), Settings, loaded from .env with sane defaults.  Everything tunable lives here (+14 more)

### Community 15 - "Community 15"
Cohesion: 0.5
Nodes (1): Scoring: competition, search volume, and the combined opportunity score.  Every

### Community 16 - "Community 16"
Cohesion: 1.0
Nodes (1): Which env vars are unset, for an error message worth reading.

### Community 18 - "Community 18"
Cohesion: 1.0
Nodes (1): Whether adopting this fit is actually justified.          Compared on the **cr

### Community 19 - "Community 19"
Cohesion: 1.0
Nodes (1): How much better, on the fairest comparison available.

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (1): Re-read .env and the environment into the module-level `settings`.

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): Open a connection with the pragmas this tool assumes everywhere.      WAL + a ge

### Community 22 - "Community 22"
Cohesion: 1.0
Nodes (1): Connection context manager that always closes.

### Community 23 - "Community 23"
Cohesion: 1.0
Nodes (1): Explicit BEGIN/COMMIT, rolling back on any exception.      Connections are opene

### Community 24 - "Community 24"
Cohesion: 1.0
Nodes (1): Split a migration script into individual statements.      Deliberately naive: a

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (1): Apply every pending migration. Returns the versions applied.

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (1): Create the database if needed and bring it up to the latest schema.

### Community 27 - "Community 27"
Cohesion: 1.0
Nodes (1): How many rows a keyword owns, for showing what a delete would destroy.      A

### Community 28 - "Community 28"
Cohesion: 1.0
Nodes (1): Permanently delete a keyword and everything recorded about it.      Returns th

### Community 29 - "Community 29"
Cohesion: 1.0
Nodes (1): Everything one refresh learned about one keyword.      Component values are wr

### Community 30 - "Community 30"
Cohesion: 1.0
Nodes (1): Every snapshot with the stored components a re-score needs.      Joins in `key

### Community 31 - "Community 31"
Cohesion: 1.0
Nodes (1): (keyword, country) -> (value, censored) for one demand source.      Loaded onc

### Community 32 - "Community 32"
Cohesion: 1.0
Nodes (1): Overwrite only the finals. Measured components and raw observations are     the

### Community 33 - "Community 33"
Cohesion: 1.0
Nodes (1): Oldest first, which is the order trend charts want.

### Community 34 - "Community 34"
Cohesion: 1.0
Nodes (1): Every keyword with its most recent snapshot, sorted.      `include_unscored` k

### Community 35 - "Community 35"
Cohesion: 1.0
Nodes (1): Replace the ranking for one (keyword, capture).      Replaces rather than appe

### Community 36 - "Community 36"
Cohesion: 1.0
Nodes (1): The most recent ranking, joined to cached app metadata.

### Community 37 - "Community 37"
Cohesion: 1.0
Nodes (1): Where one app sits across every tracked keyword, latest vs previous.      `pre

### Community 38 - "Community 38"
Cohesion: 1.0
Nodes (1): Upsert measured search terms. Returns the number of rows written.      Re-pull

### Community 39 - "Community 39"
Cohesion: 1.0
Nodes (1): Impressions per (term, country), summed across campaigns and windows.      Sum

### Community 40 - "Community 40"
Cohesion: 1.0
Nodes (1): One measured demand value for a keyword in a storefront.

### Community 41 - "Community 41"
Cohesion: 1.0
Nodes (1): Upsert demand observations. Re-importing a source replaces its values.

### Community 42 - "Community 42"
Cohesion: 1.0
Nodes (1): One vendor's difficulty rating for a keyword in a storefront.      No `censore

### Community 43 - "Community 43"
Cohesion: 1.0
Nodes (1): Upsert competition observations. Re-importing a source replaces its values.

### Community 44 - "Community 44"
Cohesion: 1.0
Nodes (1): Join vendor difficulty to the latest stored components per keyword.      The e

### Community 45 - "Community 45"
Cohesion: 1.0
Nodes (1): The highest-priority demand source that actually has uncensored rows.      Cal

### Community 46 - "Community 46"
Cohesion: 1.0
Nodes (1): Available sources with their scale and row count, richest first.

### Community 47 - "Community 47"
Cohesion: 1.0
Nodes (1): Join measured demand to the latest ladder observation per keyword.      This i

### Community 48 - "Community 48"
Cohesion: 1.0
Nodes (1): (proxy_score, measured_value) for keywords carrying both.      The input to `s

### Community 49 - "Community 49"
Cohesion: 1.0
Nodes (1): Store a fitted bridge. Never updates in place — bridges are append-only.

### Community 50 - "Community 50"
Cohesion: 1.0
Nodes (1): The newest bridge of one metric for this storefront, or None.

### Community 51 - "Community 51"
Cohesion: 1.0
Nodes (1): Each keyword's latest scores against its scores `days` ago.      The baseline

### Community 52 - "Community 52"
Cohesion: 1.0
Nodes (1): Keep every test off the real database and off the real clock.      Two things th

### Community 53 - "Community 53"
Cohesion: 1.0
Nodes (1): A migrated, throwaway database.

### Community 54 - "Community 54"
Cohesion: 1.0
Nodes (1): The reproducibility promise, at the storage layer.

### Community 55 - "Community 55"
Cohesion: 1.0
Nodes (1): An unmeasured keyword is not a high-opportunity one.

### Community 56 - "Community 56"
Cohesion: 1.0
Nodes (1): Sort keys are interpolated into SQL, so the allowlist matters.

### Community 57 - "Community 57"
Cohesion: 1.0
Nodes (1): Re-running a refresh against a cached SERP must not duplicate rows.

### Community 58 - "Community 58"
Cohesion: 1.0
Nodes (1): Falling out of the ranking is the movement most worth seeing.

### Community 59 - "Community 59"
Cohesion: 1.0
Nodes (1): Two refreshes in one afternoon must not read as a week-over-week move.

### Community 60 - "Community 60"
Cohesion: 1.0
Nodes (1): Zero would claim 'measured, didn't move'. This is 'not measured then'.

### Community 61 - "Community 61"
Cohesion: 1.0
Nodes (1): The baseline and the latest are the same row; comparing gives nothing.

### Community 62 - "Community 62"
Cohesion: 1.0
Nodes (1): A big drop is as interesting as a big rise.

### Community 63 - "Community 63"
Cohesion: 1.0
Nodes (1): A failed fetch has no opportunity score and is not a movement.

## Ambiguous Edges - Review These
- `Apple popularity endpoint (app-ads.apple.com/reporting/graphql)` → `Open: endpoint path, body and auth are inferred, not captured`  [AMBIGUOUS]
  docs/superpowers/specs/2026-08-09-apple-popularity-design.md · relation: conceptually_related_to

## Knowledge Gaps
- **276 isolated node(s):** `Streamlit dashboard.      streamlit run dashboard.py  Five views: ad-hoc key`, `sqlite3.Row -> DataFrame, keeping columns even when there are no rows.      An`, `The three headline scores, laid out identically everywhere they appear.`, `Competition components beside their weights.      The weights are shown becaus`, `Score a keyword live, without tracking it.      The one screen here that touch` (+271 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 15`** (4 nodes): `__init__.py`, `__init__.py`, `__init__.py`, `Scoring: competition, search volume, and the combined opportunity score.  Every`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 16`** (1 nodes): `Which env vars are unset, for an error message worth reading.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 18`** (1 nodes): `Whether adopting this fit is actually justified.          Compared on the **cr`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 19`** (1 nodes): `How much better, on the fairest comparison available.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 20`** (1 nodes): `Re-read .env and the environment into the module-level `settings`.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 21`** (1 nodes): `Open a connection with the pragmas this tool assumes everywhere.      WAL + a ge`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 22`** (1 nodes): `Connection context manager that always closes.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 23`** (1 nodes): `Explicit BEGIN/COMMIT, rolling back on any exception.      Connections are opene`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 24`** (1 nodes): `Split a migration script into individual statements.      Deliberately naive: a`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 25`** (1 nodes): `Apply every pending migration. Returns the versions applied.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (1 nodes): `Create the database if needed and bring it up to the latest schema.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 27`** (1 nodes): `How many rows a keyword owns, for showing what a delete would destroy.      A`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 28`** (1 nodes): `Permanently delete a keyword and everything recorded about it.      Returns th`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 29`** (1 nodes): `Everything one refresh learned about one keyword.      Component values are wr`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 30`** (1 nodes): `Every snapshot with the stored components a re-score needs.      Joins in `key`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 31`** (1 nodes): `(keyword, country) -> (value, censored) for one demand source.      Loaded onc`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 32`** (1 nodes): `Overwrite only the finals. Measured components and raw observations are     the`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 33`** (1 nodes): `Oldest first, which is the order trend charts want.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 34`** (1 nodes): `Every keyword with its most recent snapshot, sorted.      `include_unscored` k`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 35`** (1 nodes): `Replace the ranking for one (keyword, capture).      Replaces rather than appe`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 36`** (1 nodes): `The most recent ranking, joined to cached app metadata.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 37`** (1 nodes): `Where one app sits across every tracked keyword, latest vs previous.      `pre`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 38`** (1 nodes): `Upsert measured search terms. Returns the number of rows written.      Re-pull`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 39`** (1 nodes): `Impressions per (term, country), summed across campaigns and windows.      Sum`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 40`** (1 nodes): `One measured demand value for a keyword in a storefront.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 41`** (1 nodes): `Upsert demand observations. Re-importing a source replaces its values.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 42`** (1 nodes): `One vendor's difficulty rating for a keyword in a storefront.      No `censore`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 43`** (1 nodes): `Upsert competition observations. Re-importing a source replaces its values.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 44`** (1 nodes): `Join vendor difficulty to the latest stored components per keyword.      The e`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (1 nodes): `The highest-priority demand source that actually has uncensored rows.      Cal`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (1 nodes): `Available sources with their scale and row count, richest first.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (1 nodes): `Join measured demand to the latest ladder observation per keyword.      This i`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (1 nodes): `(proxy_score, measured_value) for keywords carrying both.      The input to `s`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (1 nodes): `Store a fitted bridge. Never updates in place — bridges are append-only.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 50`** (1 nodes): `The newest bridge of one metric for this storefront, or None.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 51`** (1 nodes): `Each keyword's latest scores against its scores `days` ago.      The baseline`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 52`** (1 nodes): `Keep every test off the real database and off the real clock.      Two things th`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 53`** (1 nodes): `A migrated, throwaway database.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 54`** (1 nodes): `The reproducibility promise, at the storage layer.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 55`** (1 nodes): `An unmeasured keyword is not a high-opportunity one.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 56`** (1 nodes): `Sort keys are interpolated into SQL, so the allowlist matters.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 57`** (1 nodes): `Re-running a refresh against a cached SERP must not duplicate rows.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 58`** (1 nodes): `Falling out of the ranking is the movement most worth seeing.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 59`** (1 nodes): `Two refreshes in one afternoon must not read as a week-over-week move.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 60`** (1 nodes): `Zero would claim 'measured, didn't move'. This is 'not measured then'.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 61`** (1 nodes): `The baseline and the latest are the same row; comparing gives nothing.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 62`** (1 nodes): `A big drop is as interesting as a big rise.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 63`** (1 nodes): `A failed fetch has no opportunity score and is not a movement.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Apple popularity endpoint (app-ads.apple.com/reporting/graphql)` and `Open: endpoint path, body and auth are inferred, not captured`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `Fetcher` connect `Community 2` to `Community 1`, `Community 3`, `Community 5`, `Community 7`, `Community 11`?**
  _High betweenness centrality (0.096) - this node is a cross-community bridge._
- **Why does `get()` connect `Community 1` to `Community 0`, `Community 3`, `Community 4`, `Community 5`, `Community 7`, `Community 9`, `Community 10`, `Community 11`, `Community 13`?**
  _High betweenness centrality (0.070) - this node is a cross-community bridge._
- **Why does `calibrate()` connect `Community 4` to `Community 1`, `Community 9`?**
  _High betweenness centrality (0.064) - this node is a cross-community bridge._
- **Are the 170 inferred relationships involving `Fetcher` (e.g. with `Typer entrypoint.      aso init     aso check "meditation timer"          # s` and `Make stdout/stderr able to print any keyword we might have stored.      Window`) actually correct?**
  _`Fetcher` has 170 INFERRED edges - model-reasoned connections that need verification._
- **Are the 136 inferred relationships involving `ChartIndex` (e.g. with `LookupResult` and `Ad-hoc keyword lookup: score a keyword without tracking it.  The same thing `aso`) actually correct?**
  _`ChartIndex` has 136 INFERRED edges - model-reasoned connections that need verification._
- **Are the 120 inferred relationships involving `ITunesClient` (e.g. with `Typer entrypoint.      aso init     aso check "meditation timer"          # s` and `Make stdout/stderr able to print any keyword we might have stored.      Window`) actually correct?**
  _`ITunesClient` has 120 INFERRED edges - model-reasoned connections that need verification._