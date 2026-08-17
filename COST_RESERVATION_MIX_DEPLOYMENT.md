# Cost reservation mix deployment

Two new cost datasets let the Cost Data page charge cleaning per room category
and guest count, and apply the distribution rulebook's per-origin, per-agency
and per-rate percentages. Until they are imported the page keeps its previous
figures and says so in a flag, so this can be deployed and then imported.

## What was added

| Piece | File |
| --- | --- |
| Tables | `sql/migrations/016_cost_reservation_mix.sql` |
| Upserts | `sql/import/upsert_departure_mix_data.sql`, `sql/import/upsert_distribution_mix_data.sql` |
| Export builders | `services/cost_mix_export_service.py` |
| Pipeline datasets | `departure_mix`, `distribution_mix` in `shared/pipeline.py` |
| Read queries | `cleaningDepartures`, `distributionRates` in `queries/cost_data.py` |

Both tables hold a **mix, not a level**. `functions.arr_dep_data` remains the
authority on how many departures a day had, and
`functions.room_revenue_night_data` on how much room revenue it earned. The
mixes only say how to apportion those totals across the dimensions the rulebook
is written in terms of, so a mix that is slightly out cannot move a total on the
statement - only the rate applied to it.

## Deployment steps

1. Deploy the application. Migration `016_cost_reservation_mix` is applied
   lazily under the same advisory lock as every other cost migration, so no
   manual DDL step is needed. It is safe to apply by hand first.
2. Run the cost import (`CostDataTimer` at 00:05, or
   `POST /api/costdata/import` with `{"dataset": "all"}` and the function key).
3. Reload Cost Data. The two flags below should disappear:
   - *"cleaning cost is the flat average of its configured category and
     occupancy rows"*
   - *"only the fallback distribution % was applied"*

If they persist after an import, check the App Insights warnings from
`cost_mix_export_service` - see **Source column mapping** below.

## Source column mapping

The export SQL for both datasets is **built at run time** from
`information_schema`, not read from a file. The Mews mirror's naming for
reservation origin, travel agency, rate, room category and guest counts is not
knowable from this repository, and an `UndefinedColumn` in a static export would
fail the whole nightly import and take the five working datasets down with it.

A builder that cannot resolve what it needs logs a warning naming the candidates
it tried and returns `None`. The dataset then imports nothing, and the page keeps
its previous figure and its flag. Candidate lists live at the top of
`services/cost_mix_export_service.py`; adding a synonym there is the fix.

Required for the departure mix: a reservation end column, a room category
foreign key, a category name column, and either adult/child counts or a single
person count. Required for the distribution mix: a reservation origin column.
Travel agency and rate are optional there - without them the rulebook still
applies at its origin level, which is the level that decides most of it.

## Import duration

`host.json` sets `functionTimeout` to 30 minutes and the queue's
`visibilityTimeout` to match. These two datasets are the only reservation-level
statements in the cost import and are the most expensive in it, so they are
registered **last** in `shared/pipeline.py`: each batch commits as it goes, so a
run that hits the ceiling has already imported every total the statement needs
and loses only that night's mix, which the next run replaces.

They are also the only bounded exports. `COST_MIX_WINDOW_DAYS` (default 730,
matching `COST_SOURCE_WINDOW_DAYS`) is how far back they read; the five
hotel-per-day exports still read all history. Lower it if the nightly run
approaches the ceiling. A Cost Data period older than the window falls back to
the previous behaviour and reports which revenue it applied the fallback to.

## Stale combinations

A mix row is keyed by its dimensions, so a combination that stops occurring on a
day - a cancelled reservation being the last one from some agency - has no row
for the upsert to overwrite and would keep its old figure for good. The upserts
stamp `last_seen_at` on every touch and each run deletes anything inside the
window still carrying an older stamp. That is why both tables carry an index on
`last_seen_at`, and why `transfer_dataset` takes a `clock_timestamp()` before
the load rather than a `now()` that could tie with the first batch's transaction.
