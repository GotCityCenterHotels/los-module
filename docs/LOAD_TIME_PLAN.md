# Load time plan: the road to 500ms

Audit date: 2026-08-18. Six parallel audits (cold start, per-request floor,
browser critical path, SQL layer, serving architecture, repo hygiene), 79
findings, each one adversarially verified. 74 survived, 5 were refuted.

## The metric has to be split before it can be answered

"Navigation start to first meaningful data" is undefined on three of five pages.
`index.html`, `distribution.html` and `costdata.html` render no data on
navigation: `app.js` and `distribution.js` fire only `loadHotels()` on
`DOMContentLoaded` — a deliberate non-blocking warm-up — and `loadData` is bound
to a button. `costdata.js` issues no request on load at all. Only
`supplement.html` and `costdata-input.html` auto-load.

So there are four separate questions, with four different answers.

| Metric | Verdict against 500ms |
|---|---|
| Navigation → interactive shell, all five pages | **Yes, today.** ~167ms cold browser cache, ~48ms warm |
| Click → data, Average LOS / Distribution, month grain | **Yes, after the LOS rollup.** 2,101ms → ~214ms |
| Click → data, Average LOS / Distribution, **day** grain | **No.** ~1,600ms floor; needs table windowing |
| Click → data, Cost Data, cold publication | **No.** 1,009ms → ~620ms; last 120ms needs a wide table |
| Click → data, Cost Data, repeat | **Yes.** ~40ms revalidation |
| **First visit after idle, any page** | **No, by 4–6×.** Cold start is 2,000–3,000ms |

Cold start is the binding constraint and only ~260–420ms of it is
code-addressable. The rest is platform allocation, blob mount, host start, and
the `azure.functions` + `psycopg` import graph that every route needs. **No
arrangement of application code reaches 500ms on a cold instance.**

## The LOS millisecond budget, warm backend, month grain

The default full-year view, read model on, ~170k fact rows.

| Component | Now | After |
|---|---:|---:|
| SWA linked-backend proxy hop | 15 | 15 |
| Publication lookup (0 inside the 5s worker TTL) | 6 | 6 |
| Index-only range scan + PG wire | 150 | 70 |
| psycopg `dict_row` materialisation | 210 | 15 |
| Python row reshape loop | 610 | 20 |
| `json.dumps` + utf-8 encode (24.8MB body) | 445 | 15 |
| gzip level 5 | 185 | 8 |
| Wire, 1.18MB gz @ 50Mbps | 190 | 8 |
| Browser `JSON.parse` | 230 | 12 |
| `calculateAverageView` + render (432 rows) | 60 | 45 |
| **Total** | **2,101** | **214** |

Every large term is a consequence of one decision: the server ships the storage
grain and the browser reduces it. Rolling the date dimension up in SQL collapses
all of them at once.

## Applied in this change

- **`.funcignore` added.** There was none, so `frontend/`, `tests/`, docs and
  operator scripts all shipped. Package ~3.3MB → ~600KB. `sql/` is deliberately
  kept: the schema services read migrations off disk on each worker's first
  request and `shared/sql_runner.py` reads `sql/export/` and `sql/import/` for
  every dataset.
- **CI zip exclusion fixed.** `-x "__pycache__/*"` is matched against the whole
  archive path, so it only ever excluded a root-level `__pycache__`.
- **Bytecode is now compiled and shipped on purpose.** A read-only Flex mount
  cannot have `.pyc` *written* to it, which is exactly why the package must
  *carry* them — otherwise every cold start recompiles ~417KB of source and
  discards the result. `compileall --invalidation-mode unchecked-hash` survives
  the zip round trip, which timestamp invalidation would not. Worth 145–190ms —
  **and worth nothing unless `PYTHON_VERSION` in the workflow matches the
  Function App's configured runtime**, because a `.pyc` from another minor
  version is silently ignored. The package step now fails if no bytecode made it
  in.
- **`/api/los/facts` validates before it queries.** The route resolved its ETag
  from `run_id`/`published_at` — but only after running the full query and
  shaping 170k rows, so a 304 cost as much as a 200. It now resolves the
  publication on its own (one indexed single-row read, cached 5s) and answers
  `If-None-Match` before touching a fact row. Saves 1,000–1,900ms on every
  revalidation.
- **`/api/los/facts` got a server-side byte cache.** It had a validator but no
  cache, so two browsers, two tabs, or the two sibling pages each paid the whole
  build. The Cost Data machinery is now one shared `VersionedResponseCache`
  rather than a sixth copy of the JSON+gzip+ETag+304 pattern.
- **`infra/configure-performance.ps1` fixed.** Two real bugs:
  `--always-ready-instances` is not a flag on `scale config set` — it is its own
  command group — so the script could be run with `-AlwaysReadyHttpInstances 1`
  and change nothing; and `$ErrorActionPreference` does not apply to native
  commands, so every failed `az` call printed and continued, then the read-back
  at the end displayed the configuration it had failed to apply. Also corrected
  two false comments (see below).
- **`local.settings.json` untracked and gitignored**, with a `.example` kept.
  `.gitignore` only had the Django `local_settings.py`. No real secrets were in
  it, but that is the file Azure tooling expects connection strings in.

### Two comments that were asserting false things

`configure-performance.ps1` claimed "each in-flight HTTP request holds at most
one connection per database, so matching MAX_SIZE to concurrency means no
request ever waits on a pool slot." One Cost Data request holds **three** of the
four `cost_pool` connections — `cost_data_service.py` fans seven dataset queries
across a 3-wide executor — plus checkouts for the publication pointer and the
rulebook. With `perInstanceConcurrency=4`, a second concurrent Cost Data request
contends and a third can block for `DB_POOL_ACQUIRE_TIMEOUT_SECONDS`.

It also claimed `MIN_SIZE=1` "only pays off with an always-ready instance." Both
pools are built with `open=True` at module import, so `min_size=1` fills the
first connection on a background thread while the rest of the import graph
loads — the handshake overlaps startup instead of being charged to the first
request. Always-ready is what makes it persist *between* visits.

## Remaining work, in priority order

### Tier 1 — today, no new Azure resources

| Change | Saves | Effort |
|---|---:|---|
| Delete the four CSS entry animations holding Cost Data figures at `opacity: 0` after the data is in the DOM | 150ms to legible | trivial |
| Cast dates/timestamps/numerics to text in the seven cost SQL statements so `_json_value`'s isinstance branches never fire | 115ms | small |
| `APP_DB_POOL_MIN_SIZE=1` (now in the script; needs applying) | 25–45ms per worker | trivial |
| Minify JS and CSS so `styles.css` fits the initial congestion window | 28–30ms FCP | small |

### Tier 2 — today, medium effort

| Change | Saves | Effort |
|---|---:|---|
| **Roll the LOS date dimension up server-side**: add `grain` to `/api/los/facts`, `GROUP BY date_trunc`, keep `los` and `enterprise_id` | **1,887ms** | medium |
| Defer the four service modules no request calls (retarget ~40 test patches in the same commit) | 60–145ms cold | medium |
| Collapse the 21-round-trip schema bootstrap into one autocommit probe | 25–35ms per worker | small |
| Index the `distributionRates` rulebook lateral; submit the heaviest dataset first | 110ms | small |
| Autovacuum `reservation_los_daily` after publication; cut retention 8 → 2 generations | 30–120ms | small |
| Pipeline `_read_all_cost_settings`'s 7 serial statements | 12ms | small |

The LOS rollup is the one change that decides whether the click-to-data metric
passes. It has a UX consequence worth stating plainly: grain switching becomes a
refetch (~200ms) instead of a local re-render. That is the trade for a 2,101ms →
214ms first load.

**Day grain cannot reach 500ms.** Day *is* the storage grain of
`reservation_los_daily`, so a date rollup is an identity transform there. A full
portfolio year at day grain stays ~1,600ms even after dropping the redundant
per-row `enterpriseId`/`hotelName`, and needs the table render windowed — a
separate ~1,100ms cliff. Do not promise 500ms at day grain.

### Tier 3 — needs owner approval and spend

| Change | Saves | Note |
|---|---:|---|
| One always-ready HTTP instance | **1,700–2,600ms** on first visit after idle | Standing charge. Warms 1 of up to 10 instances, and `perInstanceConcurrency=4` means a 5th concurrent request still spills to a cold instance |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | 0 directly | **Nothing in this plan can be verified without it** |
| Materialise the six import-derived cost datasets into one wide table | ~250ms + most of the 168ms encode | **Flagged, not recommended yet.** Three `distributionRates` columns are rulebook-derived and a Cost Input save advances the publication with no import, so an import-time build would serve a stale `matched_percent` as fresh |

## Rejected

- **Azure Cache for Redis** for a shared response cache. A `bytea` response
  table in Database A is the only version worth building, and only after the
  payload shrinks — a shared-cache hit saves ~55–75ms against ~30–40ms of added
  lookup, which is not worth new infrastructure.
- **Edge/CDN caching of API responses via SWA route headers.** Every read route
  sends `Cache-Control: private`, and SWA does not cache linked-backend
  responses regardless of headers.
- **Azure Front Door** in front of both origins. The SWA linked-backend proxy
  hop is real but only ~5–20ms on a warm socket.
- **Precomputed blob artifacts** keyed by publication version, as originally
  framed. It cannot be served same-origin on Static Web Apps, and the browser
  still parses the same bytes — so it is worth nothing until the payload shape
  is fixed. Revisit after the LOS rollup.
- **Splitting `styles.css` per page.** 57–90% of it is unreachable on any given
  page, but the file is 18.3KB brotli and splitting costs more in cache misses
  across navigations than it saves. Minify instead.

## Measurement caveat

There is no `APPLICATIONINSIGHTS_CONNECTION_STRING` on the app, so no cold-start
telemetry exists. Every cold figure here is an estimate: a live TTFB probe plus
reproduced desktop CPU, derated ~1.45× for a 1-vCPU 2048MB Flex instance. Get
that connection string in place before signing off on any number in this
document.

Nothing in the repo measures end-to-end page load. Every threshold that exists —
`MAX_QUERY_RANGE_DAYS`, the 40s client deadline, `statement_timeout` — is
calibrated to avoid the 45s Static Web Apps proxy timeout, not to hold 500ms.
A gate calibrated to the actual target is what stops this regressing again.
