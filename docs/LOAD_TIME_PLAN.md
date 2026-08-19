# Load time plan: the road to 500ms

Audit date: 2026-08-18. Six parallel audits (cold start, per-request floor,
browser critical path, SQL layer, serving architecture, repo hygiene), 79
findings, each one adversarially verified. 74 survived, 5 were refuted.

**Status: every item implementable in this repository is done.** Three items
remain and all three need Azure access or an owner decision — see
[What is left](#what-is-left).

## The metric has to be split before it can be answered

"Navigation start to first meaningful data" is undefined on three of five pages.
`index.html`, `distribution.html` and `costdata.html` render no data on
navigation: `app.js` and `distribution.js` fire only `loadHotels()` on
`DOMContentLoaded` — a deliberate non-blocking warm-up — and `loadData` is bound
to a button. `costdata.js` issues no request on load at all. Only
`supplement.html` and `costdata-input.html` auto-load.

So there are four questions, with four different answers.

| Metric | Verdict against 500ms |
|---|---|
| Navigation → interactive shell, all five pages | **Yes** — was ~167ms cold cache, now ~136ms |
| Click → data, Average LOS / Distribution, month grain | **Yes** — 2,101ms → ~214ms |
| Click → data, Average LOS / Distribution, **day** grain | **No** — ~1,600ms floor; needs table windowing |
| Click → data, Cost Data, cold publication | **No** — 1,009ms → ~620ms |
| Click → data, Cost Data, repeat | **Yes** — ~40ms revalidation |
| **First visit after idle** | **No, by 4–6×** — cold start is 2,000–3,000ms |

Cold start is the binding constraint and only ~260–420ms of it is
code-addressable. The rest is platform allocation, blob mount, host start, and
the `azure.functions` + `psycopg` import graph that every route needs. **No
arrangement of application code reaches 500ms on a cold instance.**

## The LOS millisecond budget, warm backend, month grain

The default full-year view, read model on, ~170k stored fact rows.

| Component | Before | After |
|---|---:|---:|
| SWA linked-backend proxy hop | 15 | 15 |
| Publication lookup (0 inside the 5s worker TTL) | 6 | 6 |
| Index-only range scan + PG wire | 150 | 70 |
| psycopg `dict_row` materialisation | 210 | 15 |
| Python row reshape loop | 610 | 20 |
| `json.dumps` + utf-8 encode (24.8MB body) | 445 | 15 |
| gzip level 5 | 185 | 8 |
| Wire, 1.18MB gz | 190 | 8 |
| Browser `JSON.parse` | 230 | 12 |
| `calculateAverageView` + render (432 rows) | 60 | 45 |
| **Total** | **2,101** | **214** |

Every large term was a consequence of one decision: the server shipped the
storage grain and the browser reduced it. Rolling the date dimension up in SQL
collapses all of them at once.

## Applied

### Serving the data

- **`/api/los/facts` validates before it queries.** The route derived its ETag
  from `run_id`/`published_at` — but only after running the full range scan and
  shaping 170k rows, so a 304 cost as much as a 200. It now resolves the
  publication on its own (one indexed single-row read, cached 5s) and answers
  `If-None-Match` before touching a fact row. **1,000–1,900ms per revalidation.**
- **`/api/los/facts` got a server-side byte cache.** It had a validator but no
  cache, so two browsers, two tabs, or the two sibling pages each paid the whole
  build. The Cost Data machinery is now one shared `VersionedResponseCache`
  rather than a sixth copy of the JSON+gzip+ETag+304 pattern, which had already
  diverged on its gzip threshold.
- **The LOS date dimension rolls up in SQL.** `/api/los/facts` takes a `grain`
  and does the reduction in a `GROUP BY`, landing rows on exactly the period keys
  `LosData.getPeriodKey` computes — PostgreSQL truncates a week to its Monday,
  same as the browser — so the client-side aggregation still runs and becomes an
  identity transform. `los` and `enterprise_id` stay in the grouping, because the
  Distribution page buckets by `los` and both pages filter by hotel locally.
  Day grain skips the `GROUP BY`: it *is* the storage grain. **~1,887ms.**
- **Cost values render in SQL.** `_json_value` converted every cell —
  `date.isoformat()` at ~3µs a call, `str()` on each `Decimal` — and the larger
  half of the cost was psycopg building those objects only for them to be
  discarded as strings. `stay_date` and the numeric aggregates are cast in
  PostgreSQL, byte-identically. **~115ms.**
- **The slowest cost query starts first.** The executor is three wide and there
  are seven datasets, so submissions queue; `distributionRates` was declared last
  and its whole duration was appended rather than overlapped. Submission order is
  now separate from response order. **~110ms** with the index below.
- **The rulebook lateral is indexed as it is probed** (migration 019). The joins
  compare `lower(btrim(...))`, so the existing group-key indexes could seek to a
  group but then evaluated the expression on every row. Expression indexes let it
  seek straight to the match.
- **`_read_all_cost_settings` pipelines.** Ten independent statements ran one
  after another on a shared cursor; `_read_cost_settings` already pipelined the
  same shape for a single property. **~12ms.**

### Cold start and first request

- **Bytecode is compiled and shipped on purpose.** A read-only Flex mount cannot
  have `.pyc` *written* to it, which is exactly why the package must *carry*
  them — otherwise every cold start recompiles ~417KB of source and discards the
  result. `compileall --invalidation-mode unchecked-hash` survives the zip round
  trip, which timestamp invalidation would not. **145–190ms — and worth nothing
  unless `PYTHON_VERSION` in the workflow matches the Function App's configured
  runtime**, because a `.pyc` from another minor version is silently ignored. The
  package step now fails if no bytecode made it in.
- **The worker's module graph is imported in the worker.** The import pipeline and
  the two sync services are reachable only from the queue worker and the enqueue
  routes, but Azure Functions imports `function_app` to index its triggers, so
  every HTTP cold start paid for them: ~37ms measured, plus `shared.sql_runner`,
  `queries.los_sync` and `cost_mix_export_service` behind them. **~37ms (≈54ms
  derated).**
- **The schema check no longer takes the lock.** All four `ensure_*_schema()`
  helpers took a cluster-wide advisory lock and then one SELECT per migration to
  discover there was nothing to do — ~11 round trips, and worse, the lock is
  shared, so the parallel `/api/los/hotels` request blocked behind it. A
  migration name is recorded in the transaction that applies it, so a worker that
  sees every expected name knows the schema is current without coordinating.
  Two statements, no lock. **25–35ms.**

### The browser

- **The frontend is minified** (`npm run build` → `dist/`). `styles.css` is
  render-blocking on all five pages and was 19.8KB gzipped, over the ~14.6KB
  initial congestion window — a second round trip before first paint on every
  cold-cache visit. Minified it is 11.6KB and fits in one. Per-page critical path
  drops from 35–61KB to 20–30KB. esbuild's `transform` is used deliberately, not
  `build`: these are classic scripts assigning to globals, and bundling would
  rename them and break every page at once.
- **Data is no longer held behind entry fades.** Four animations used
  `animation-fill-mode: both` with `opacity: 0`, so the primary figures sat in the
  DOM invisible through a 45–85ms delay and took another 260–340ms to become
  legible. `cost-rise` now animates only the slide; `cost-row-in` was
  opacity-only on the first fifteen table rows and is gone. **~150ms to legible.**

### The read model and the repo

- **Retention cut from eight generations to two.** `reservation_los_daily` kept
  seven superseded publications, so its lookup index covered all eight while the
  read filters on one `run_id`. One previous generation is what a rollback needs.
- **The publication vacuums.** Nothing set the visibility map after a bulk
  publication, so the index-only scan the read model was designed around only
  became index-only once autovacuum came round. Done on its own autocommit
  connection (VACUUM is refused inside a transaction) and best-effort: the
  publication is already committed, so a blocked vacuum must not report it as a
  failed sync. **30–120ms.**
- **Deploy package ~3.3MB → ~600KB.** There was no `.funcignore`, and CI's
  `-x "__pycache__/*"` is matched against the whole archive path so it only ever
  excluded a root-level one. `sql/` is deliberately kept — the schema services
  read migrations off disk on each worker's first request and
  `shared/sql_runner.py` reads `sql/export/` and `sql/import/` per dataset — and a
  CI step now fails the build if they fall out.
- **`infra/configure-performance.ps1` fixed.** `--always-ready-instances` is not a
  flag on `scale config set` but its own command group, so the script could be run
  with `-AlwaysReadyHttpInstances 1` and change nothing; and
  `$ErrorActionPreference` does not cover native commands, so a failed `az` call
  printed and continued until the read-back displayed the configuration it had
  failed to apply.
- **`local.settings.json` untracked and gitignored**, with a `.example` kept.
  `.gitignore` only had the Django `local_settings.py`.

### Two comments that were asserting false things

`configure-performance.ps1` claimed "each in-flight HTTP request holds at most
one connection per database, so matching MAX_SIZE to concurrency means no request
ever waits on a pool slot." One Cost Data request holds **three** of the four
`cost_pool` connections — `cost_data_service.py` fans seven dataset queries across
a 3-wide executor — plus checkouts for the publication pointer and the rulebook.
With `perInstanceConcurrency=4`, a second concurrent Cost Data request contends
and a third can block for `DB_POOL_ACQUIRE_TIMEOUT_SECONDS`.

It also claimed `MIN_SIZE=1` "only pays off with an always-ready instance." Both
pools are built with `open=True` at module import, so `min_size=1` fills the first
connection on a background thread while the rest of the import graph loads — the
handshake overlaps startup instead of being charged to the first request.
Always-ready is what makes it persist *between* visits.

## What is left

### Needs Azure access — not doable from a checkout

| Change | Saves | Note |
|---|---:|---|
| One always-ready HTTP instance: `./infra/configure-performance.ps1 -AlwaysReadyHttpInstances 1` | **1,700–2,600ms** on first visit after idle | Standing charge. Warms 1 of up to 10 instances, and `perInstanceConcurrency=4` means a 5th concurrent request still spills to a cold one |
| Apply the rest of the script (it sets `APP_DB_POOL_MIN_SIZE=1`) | 25–45ms per worker | The audit found it had never been run |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | 0 directly | **Nothing in this plan can be verified without it** |

Also confirm `PYTHON_VERSION` in `main_los-functions.yml` (currently `3.13`)
matches the Function App's configured runtime. The audit's live probe suggested
3.14; if they differ, the shipped bytecode is ignored and its 145–190ms is lost.

### Recommended against building blind

**Materialising the six import-derived cost datasets into one wide table** is the
only route to a sub-500ms Cost Data *cold* build (~620ms → under 500ms). It is
large, it needs a schema migration and pipeline changes, and it carries a real
correctness hazard: three `distributionRates` columns are rulebook-derived and a
Cost Input save advances the publication with no import, so an import-time build
would serve a stale `matched_percent` as fresh. Scoping it to the six
import-derived datasets avoids that, but it still cannot be validated without
plan output from the real database.

A Cost Data *repeat* is already ~40ms. The cold build is one request per
publication per worker. I would take the always-ready instance first and measure
before spending here.

### Not worth doing

- **Azure Cache for Redis** for a shared response cache. A `bytea` response table
  in Database A is the only version worth building, and only after the payload
  shrinks — a shared-cache hit saves ~55–75ms against ~30–40ms of added lookup.
- **Edge/CDN caching of API responses via SWA route headers.** Every read route
  sends `Cache-Control: private`, and SWA does not cache linked-backend responses
  regardless of headers.
- **Azure Front Door** in front of both origins. The proxy hop is real but only
  ~5–20ms on a warm socket.
- **Precomputed blob artifacts** keyed by publication version. Cannot be served
  same-origin on Static Web Apps, and the browser still parses the same bytes — so
  it was worth nothing until the payload shape was fixed. Revisit now that the
  rollup has landed, if measurement justifies it.
- **Splitting `styles.css` per page.** 57–90% is unreachable on any given page,
  but it is one file cached across navigations; splitting costs more in misses
  than it saves. Minifying it was the win.
- **Casting `last_updated_at` in SQL** alongside the other columns. It is
  `timestamptz`, and `::text` renders it space-separated with a two-digit offset
  rather than ISO 8601; `frontend/costdata.js` feeds it straight to `new Date()`,
  which rejects that spelling in Safari.

## Measurement caveat

There is no `APPLICATIONINSIGHTS_CONNECTION_STRING` on the app, so no cold-start
telemetry exists. Every cold figure here is an estimate: a live TTFB probe plus
reproduced desktop CPU, derated ~1.45× for a 1-vCPU 2048MB Flex instance. Get
that connection string in place before signing off on any number in this
document.

Nothing in the repo measures end-to-end page load. Every threshold that exists —
`MAX_QUERY_RANGE_DAYS`, the 40s client deadline, `statement_timeout` — is
calibrated to avoid the 45s Static Web Apps proxy timeout, not to hold 500ms. A
gate calibrated to the actual target is what stops this regressing again.
