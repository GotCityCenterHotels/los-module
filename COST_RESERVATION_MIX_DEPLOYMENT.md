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

Required for the departure mix, on the room-nights relation
(`staging.room_nights_source`, else `staging.room_nights_current`): `end_utc`,
`hotel_name`, `reservation_id`. Plus a room category and guest counts - looked for
on the nights first and on `reservation_current` otherwise - and a name column on
`resource_category_current`.

Against the current source none of that needs a join: `staging.room_nights_current`
already exposes both category ids and a `person_count` that is the summed
PersonCounts list, so the export reads the nights, joins
`resource_category_current` for the name, and nothing else.

### Which room category

`coalesce(assigned_resource_category_id, requested_resource_category_id)`.
Assigned is the room that was actually occupied and therefore actually cleaned;
requested is what was booked. An upgrade from a double to a suite is cleaned as a
suite. Assigned comes from a LEFT JOIN in the view and is null for a stay with no
room assigned, which is what the fallback covers.

The category *name* still comes from `resource_category_current`, not from the
view's own `requested_space_name`. The view reads that out of
`resource_category_history`, which can hold a superseded spelling, and the cleaning
rows the page matches against were saved with the name the Cost Input picker showed
- which is the current one.

### Guests in the room

Mews publishes `Reservation.PersonCounts`, which is not a number:

```json
[{"Count": 1, "AgeCategoryId": "2d7a…"}, {"Count": 3, "AgeCategoryId": "2df…"}]
```

The occupancy is the **sum of its Counts** - four in that example - so the export
sums the list rather than reading a column. Every age category counts: a child in
the room is still a bed made up, and the occupancies the Cost Input editor offers
come from a category's capacity plus its extra beds, which is a head count too.

Two readings are supported and the **declared type decides which**, not the name.
A `json`, `jsonb` or text column is summed as a list; an integer column is read as
a number. A mirror that flattened PersonCounts into an integer but kept the plural
name would otherwise be parsed as an empty list and every room costed at one
guest - wrong, and silently so. A mirror that flattened it into `adult_count` and
`child_count` is preferred over either, because adding a total that already
includes children to a child count would double the occupancy.

The sum is guarded by `jsonb_typeof`, since `jsonb_array_elements` raises on
anything that is not an array: one malformed row must not fail the import. An
absent, empty or malformed list floors at one guest.

The export also collapses room nights to one row per reservation per departure
date *before* joining or counting - `room_nights_source` holds a row per room
night, so without that the sum would be evaluated once per night of every stay,
and again as a grouping key.

Required for the distribution mix: a reservation origin column. Travel agency and
rate are optional there; without them the rulebook still applies at its origin
level, which is the level that decides most of it.

## Why the departure mix reads room_nights_source

`total_departures` in `functions.arr_dep_data` is
`count(distinct reservation_id)` over `staging.room_nights_source`, filtered on
`canceled_utc IS NULL`, with the departure date taken from `end_utc` in Stockholm
time. The mix is that same count, over that same relation, with that same filter
and the same `enterprise_current` name join - only partitioned by room category
and guest count as well.

That is deliberate and load-bearing. The cleaning line charges the authoritative
departure count at the rate the mix implies, so the mix has to sum to that count;
deriving it from `reservation_current` instead - which an earlier version did -
made the two independently computed and left no reason they should agree.

The only thing that does not come from the nights is the pair of extra
dimensions. The category and the guest counts are read from the nights when the
view carries them, and through a join to `reservation_current` when it does not.
A reservation whose category cannot be resolved drops out of the weighting rather
than being counted at zero; the total it is applied to is unaffected.

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
