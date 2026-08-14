# LOS performance report

Benchmark date: 2026-08-14. Source: production-shaped `integration_db`, queried
read-only. Baseline planner tests used 2025-08-01 through 2026-07-31; the direct
rewrite and annual parity tests used 2026-01-01 through 2026-12-31.

| Variant | Runtime | Shared hits | Reads | Heap fetches | Temp I/O | Notes |
|---|---:|---:|---:|---:|---:|---|
| Previous query, observed cold/warm | 24.8–39.4s | 895k–909k | 81k–95k | ~99k | ~2.8k blocks | View expansion and repeated probes |
| Previous query + 64MB work memory | 14.95s | 900k | 91k | 98,559 | 0 | Spill removed; probes remained |
| Nested loops disabled | 54.36s | 1.55M | 201k | 361,553 | 0 | Broad index scans regressed |
| Direct-table rewrite, first run | 4.10s | 881,227 | 5,958 | 111,284 | 0 | Read-heavy run |
| Direct-table rewrite, warm sameDate | 1.96–1.97s | 887,176 | 0 | 111,284 | 0 | Three in-memory quicksorts |
| Direct-table rewrite, warm sameWeekday | 1.92–1.95s | 886,884 | 0 | 111,288 | 0 | Three in-memory quicksorts |
| Database A publication lookup rehearsal | 32.7ms | — | — | — | 0 | Transaction rolled back |

The direct rewrite produced zero `EXCEPT ALL` differences against the previous
query for one-month and annual ranges under both comparison bases. The annual
comparison executed both complete queries and found zero differing rows.

No index, table, vacuum, partition, or persistent setting change was made in
`integration_db`. Partitioning is not recommended: reservation scanning is not
dominant and the order-item join has no useful partition-key predicate.
