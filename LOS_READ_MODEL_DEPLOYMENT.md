# LOS read model deployment

The raw LOS query remains read-only in `integration_db`. Interactive LOS reads
can be switched to the published aggregate in PostgreSQL Database A.

## Safe rollout

1. Deploy with both flags disabled:

   - `LOS_SYNC_ENABLED=false`
   - `LOS_READ_MODEL_ENABLED=false`

2. Enable synchronization only and queue the initial full job:

   - set `LOS_SYNC_ENABLED=true`;
   - `POST /api/los/import` with `{"mode":"full"}` and the Function key;
   - poll the returned import-job URL until it succeeds.

3. Check `/api/los/status`, then run `validate_los_read_model.py` for one month
   and a full year with both comparison bases. Every run must report
   `identical:true`.
4. Benchmark `/api/los/facts`; require p95 below 500ms before enabling reads.
5. Set `LOS_READ_MODEL_ENABLED=true`. The 00:20 UTC timer runs daily deltas and
   a full reconciliation each Sunday.

The read path never falls back to the raw query after the flag is enabled. A
failed refresh leaves the previous publication active. Publications older than
30 hours are reported as stale and logged as warnings.

## Rollback

Set `LOS_READ_MODEL_ENABLED=false` to restore the optimized raw query. Set
`LOS_SYNC_ENABLED=false` to stop new jobs. The additive Database A tables can
remain for diagnosis and do not require immediate removal. No rollback action
is required in `integration_db` because the implementation creates no objects
and changes no settings there.
