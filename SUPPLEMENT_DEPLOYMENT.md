# Supplement live-data deployment

Supplement uses two PostgreSQL databases with a hard directional boundary:

- Database A is the writable application PostgreSQL database. Configure it with
  `POSTGRES_*` or `COST_DB_*`.
- Database B is `integration_db`. Configure it with `INTEGRATION_DB_*`. Its role
  must have only `CONNECT`, `USAGE`, and `SELECT`; the application also forces
  `default_transaction_read_only=on` on every source session.

`DB_*` remains a backwards-compatible alias for existing LOS reads from
`integration_db`. It is never accepted by the Database A connection code.

## Required settings

```text
POSTGRES_HOST
POSTGRES_DB
POSTGRES_USER
POSTGRES_PASSWORD

INTEGRATION_DB_HOST
INTEGRATION_DB_NAME=integration_db
INTEGRATION_DB_USER
INTEGRATION_DB_PASSWORD

SUPPLEMENT_LIVE_ENABLED=true
SUPPLEMENT_TIME_ZONE=Europe/Stockholm
```

This repository deploys the Function App on Linux Flex Consumption. Do not set
`WEBSITE_TIME_ZONE` or `TZ`: Azure does not support either setting on that plan
and warns that they can affect SSL and metrics. The timer is registered for the
00:15 and 01:15 UTC candidates; application code uses `Europe/Stockholm` to
select exactly one run corresponding to 02:15 local time. During the spring DST
jump, when 02:15 does not exist, it runs at 03:15. During the autumn repeated
hour, it uses the first 02:15 occurrence.

See Microsoft's [Flex Consumption plan](https://learn.microsoft.com/azure/azure-functions/flex-consumption-plan)
and [timer trigger](https://learn.microsoft.com/azure/azure-functions/functions-bindings-timer#ncrontab-time-zones)
documentation.

Supplement does not require or discover a prebuilt source relation. Each run
performs exactly two bounded reads: booking lifecycle rows from the current
reservation/order-item relations and room inventory from current/history
resource relations. View dates and aggregates are reconstructed in Database A.
Create the source role with an integration-database administrator, substituting
the real database, schema, and login names:

```sql
REVOKE ALL ON DATABASE integration_db FROM supplement_reader;
GRANT CONNECT ON DATABASE integration_db TO supplement_reader;
GRANT USAGE ON SCHEMA public TO supplement_reader;
GRANT SELECT ON public.reservation_current, public.order_item_current,
    public.service_current, public.enterprise_current, public.resource_current,
    public.resource_category_current,
    public.resource_category_assignment_current, public.resource_history,
    public.resource_category_history,
    public.resource_category_assignment_history
TO supplement_reader;
ALTER ROLE supplement_reader SET default_transaction_read_only = on;
```

Do not grant table ownership, `CREATE`, `INSERT`, `UPDATE`, `DELETE`,
`TRUNCATE`, or sequence privileges. Synchronization also checks
`current_database()` and `transaction_read_only` before issuing a source query.

Profile both direct source queries before enabling the feature:

```powershell
python profile_supplement_source.py 2026-08-12
```

The booking plan must prune or index-filter `order_item_current.start_utc` for
the bounded stay window. The inventory plan must use indexed access to its
resource history/current tables. Do not enable the timer when the profile exits
with status 2 or either query broadly scans history. The source transaction is
read-only even when `EXPLAIN ANALYZE` is used.

## Initial load and operation

Apply the app on Database A with `SUPPLEMENT_LIVE_ENABLED=false` first, then
create the read model explicitly:

```powershell
python migrate_supplement.py
```

Migration `004_supplement_lifecycle_ids` repairs databases that had already
recorded the original name-keyed migration `003`. It clears only the obsolete,
rebuildable Supplement facts and publication pointer, then creates the UUID-keyed
lifecycle schema. Run a backfill afterward before enabling the feature.

Backfill individual snapshot dates during approved off-peak periods:

```powershell
python backfill_supplement.py 2026-08-12
```

Backfill an inclusive range with one independently committed snapshot date at a
time. This is intentionally not one large source query. A small pause can be
added to reduce sustained load on `integration_db`:

```powershell
python backfill_supplement.py 2025-08-13 2026-08-14 --pause-seconds 2
```

The command reports progress and prints the exact command to resume from the
failed or interrupted date. Re-importing a completed date is safe because its
published rows are transactionally replaced. Do not run the range backfill in
parallel. If it overlaps the 02:15 daily timer, the advisory lock permits only
one synchronization; run a manual delta after the backfill if that daily run was
skipped.

The command is resumable because every snapshot import transactionally replaces
that snapshot in Database A. Inventory before 2026-02-27 uses current inventory
and is published with `inventoryQuality=approximated-current`; inventory on or
after that date is reconstructed from resource histories. Once required
coverage and parity checks pass, set
`SUPPLEMENT_LIVE_ENABLED=true`. The guarded daily timer runs at 02:15
Europe/Stockholm and imports the latest view date plus the preceding three dates
in one booking read and one inventory read.

Manual delta or repair imports use the function-authenticated endpoint. The
endpoint returns `202 Accepted` with a job and `statusUrl`; poll that URL until
the queued worker publishes or fails the import:

```json
{"mode":"repair","snapshotFrom":"2026-08-10","snapshotTo":"2026-08-12"}
```

Failed runs never replace `functions.supplement_publication`; APIs continue to
serve the last good PostgreSQL snapshot and show a stale warning after 36 hours.

Both the daily timer and the manual endpoint enqueue work on `import-jobs`.
See `IMPORT_PERFORMANCE_DEPLOYMENT.md` for queue retry, scaling, and source-plan
verification details.

Run representative parity checks after backfill:

```powershell
python validate_supplement_parity.py 2026-08-12 <enterprise-uuid> 2026-09-01 --category <category-uuid>
```

Then benchmark the uncached, compressed all-hotel response. The command exits
with status 2 unless it completes under two seconds and under five MB gzip:

```powershell
python benchmark_supplement_grid.py --end-date 2026-08-12
```
