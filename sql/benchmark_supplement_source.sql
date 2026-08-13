/*
The Supplement source is no longer a prebuilt relation. Its two canonical,
parameterized EXPLAIN (ANALYZE, BUFFERS) statements live in
queries/supplement_source.py so profiling and synchronization cannot drift.

Run this off-peak from the repository root with the dedicated read-only
INTEGRATION_DB_* credential:

    python profile_supplement_source.py 2026-08-12

The command profiles:
  1. the start_utc-bounded reservation/order-item lifecycle extraction; and
  2. the current/history resource inventory extraction.

It exits with status 2 unless the booking query demonstrates start_utc index or
partition access and the inventory query demonstrates indexed access. The
connection also verifies current_database() = integration_db and
transaction_read_only = on before either EXPLAIN is issued.
*/
