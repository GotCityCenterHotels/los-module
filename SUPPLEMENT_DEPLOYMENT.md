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

SUPPLEMENT_SOURCE_RELATION=<schema>.<relation>
SUPPLEMENT_LIVE_ENABLED=true
WEBSITE_TIME_ZONE=W. Europe Standard Time
```

The configured source relation is read with a projection-only bounded `SELECT`
and must expose these columns:

```text
view_date
stay_date
hotel_code
space_room_name
requested_room_name
total_assigned_space
sum_price
total_space
space_to_sell
```

The projection must emit inventory rows even when a category has zero assigned
rooms; otherwise OCC and RevPAR cannot retain the correct denominator. Create
the source role with an integration-database administrator, substituting the
real database, schema, relation, and login names:

```sql
REVOKE ALL ON DATABASE integration_db FROM supplement_reader;
GRANT CONNECT ON DATABASE integration_db TO supplement_reader;
GRANT USAGE ON SCHEMA reporting TO supplement_reader;
GRANT SELECT ON reporting.supplement_revenue_snapshot TO supplement_reader;
ALTER ROLE supplement_reader SET default_transaction_read_only = on;
```

Do not grant table ownership, `CREATE`, `INSERT`, `UPDATE`, `DELETE`,
`TRUNCATE`, or sequence privileges. Synchronization also checks
`current_database()` and `transaction_read_only` before issuing a source query.

The relation name is deliberately not guessed. Obtain the concrete relation
from the integration database owner and profile it before enabling the feature:

```powershell
python profile_supplement_source.py 2026-08-12
```

The plan must use partition pruning or an index beginning with `view_date` and
must retain the bounded `stay_date` predicate. Do not enable the timer when the
profile command exits with status 2 or the plan scans unbounded history. The
profile includes the latest-snapshot discovery query as well as the bounded
snapshot extraction.

## Initial load and operation

Apply the app on Database A with `SUPPLEMENT_LIVE_ENABLED=false` first, then
create the read model explicitly:

```powershell
python migrate_supplement.py
```

Backfill individual snapshot dates during approved off-peak periods:

```powershell
python backfill_supplement.py 2026-08-12
```

The command is resumable because every snapshot import transactionally replaces
that snapshot in Database A. Once required coverage and parity checks pass, set
`SUPPLEMENT_LIVE_ENABLED=true`. The daily timer runs at 02:15 in the Function
App's configured timezone and reimports a three-day correction overlap.

Manual delta or repair imports use the function-authenticated endpoint:

```json
{"mode":"repair","snapshotFrom":"2026-08-10","snapshotTo":"2026-08-12"}
```

Failed runs never replace `functions.supplement_publication`; APIs continue to
serve the last good PostgreSQL snapshot and show a stale warning after 36 hours.

Run representative parity checks after backfill:

```powershell
python validate_supplement_parity.py 2026-08-12 "Hotel A" 2026-09-01 --category Double
```

Then benchmark the uncached, compressed all-hotel response. The command exits
with status 2 unless it completes under two seconds and under five MB gzip:

```powershell
python benchmark_supplement_grid.py --end-date 2026-08-12
```

For a Linux Function App, use `TZ=Europe/Stockholm` instead of the Windows
`WEBSITE_TIME_ZONE` value shown above.
