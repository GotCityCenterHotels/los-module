/*
Run against integration_db off-peak after replacing the relation name and dates.
Production is blocked if the plan scans unbounded history instead of pruning or
indexing on view_date and stay_date.
*/
EXPLAIN (ANALYZE, BUFFERS, SETTINGS, SUMMARY)
SELECT
    view_date::date,
    stay_date::date,
    hotel_code,
    space_room_name,
    requested_room_name,
    total_assigned_space,
    sum_price,
    total_space,
    space_to_sell
FROM reporting.supplement_revenue_snapshot
WHERE view_date >= DATE '2026-08-12'
  AND view_date < DATE '2026-08-13'
  AND stay_date >= DATE '2026-08-05'
  AND stay_date < DATE '2028-02-13';
