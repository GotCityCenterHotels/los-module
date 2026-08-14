# Import queue and performance deployment

## Runtime design

Manual HTTP imports and both daily timers only create a row in
`functions.import_jobs` and publish its `jobId` to the `import-jobs` Azure
Storage queue. `ImportJobWorker` claims that row before running the existing
cost or Supplement service. HTTP callers receive `202 Accepted` and poll
`GET /api/imports/{jobId}`; no import work remains inside Static Web Apps'
45-second request window.

Only one active cost job and one active Supplement job are allowed. Queue
deliveries are idempotent, attempts are recorded, and the Functions queue
binding retries three times before moving the message to
`import-jobs-poison`. A failed output binding can be repaired by repeating the
request: an existing `queued` job is emitted again and duplicate messages are
safe.

Migration `005_import_jobs.sql` is applied lazily under a PostgreSQL advisory
lock. It is safe to apply before deployment as well.

## Flex Consumption and PostgreSQL limits

Apply the checked-in configuration with:

```powershell
./infra/configure-performance.ps1
```

The defaults configure 10 maximum Flex instances, four concurrent HTTP
requests per instance, and two PostgreSQL pools of at most four connections
per process. If Database A and Database B share one PostgreSQL server, the
worst-case application pool allocation is therefore 80 connections. Queue
batch size is one so a worker process does not execute multiple imports at
once.

The current Azure PostgreSQL server reports `max_connections=429`. Revisit the
cap after load testing or any server/SKU change. Do not raise instance count or
pool sizes independently; recalculate their product and leave capacity for
administration and other applications.

## Supplement source plan

The historical as-of query requires the three indexes in
`sql/supplement_index/source_history_indexes.sql`. Run that script as the
`integration_db` owner outside a transaction, then profile through the
read-only application role:

```powershell
python profile_supplement_source.py 2026-08-14
```

The gate now requires index access on each history relation and rejects broad
index scans as well as sequential scans. On 2026-08-14 the live plan used all
three bounded indexes, examined 626 `resource_history` rows, and completed the
inventory query in approximately 307 ms on the first verification run and 15 ms
with warm PostgreSQL buffers. The checked-in profile records the current passing
plan.

## Release verification

1. Run `python -m unittest discover -s tests -p "test_*.py" -v`.
2. Run `npm test`.
3. Deploy the Function App before deploying the polling frontend.
4. POST `{"dataset":"parking"}` to `/api/costdata/import` and confirm `202`.
5. Poll the returned `statusUrl` through `queued`, `running`, and `succeeded`.
6. Verify the queue is empty and no message exists in `import-jobs-poison`.
7. Confirm `Import job pool stats` and completion duration logs in Azure.
