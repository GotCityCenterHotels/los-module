/*
Period-bounded hotel-list benchmark -- run manually in pgAdmin.

This represents:
  startDate=2026-01-01
  endDate=2026-12-31
  lyComparisonBasis=sameWeekday

Inspect the two source branches separately. Confirm that the Current and LY
start_utc predicates are applied at the underlying reservation scan, then
inspect actual rows, loops, shared reads, temp I/O, and sort/hash work used by
the final DISTINCT. Do not infer improvement from the SQL shape alone.
*/

EXPLAIN (
    ANALYZE,
    BUFFERS,
    VERBOSE,
    SETTINGS,
    SUMMARY
)
WITH hotel_codes AS (
    SELECT trim(r.hotel_name)::text AS hotel_code
    FROM staging.room_nights_source r
    WHERE
        r.hotel_name IS NOT NULL
        AND trim(r.hotel_name) <> ''
        AND r.start_utc >= (
            DATE '2026-01-01'::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            DATE '2027-01-01'::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )

    UNION ALL

    SELECT trim(r.hotel_name)::text AS hotel_code
    FROM staging.room_nights_source r
    WHERE
        r.hotel_name IS NOT NULL
        AND trim(r.hotel_name) <> ''
        AND r.start_utc >= (
            DATE '2025-01-02'::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            DATE '2026-01-02'::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )
)
SELECT DISTINCT hotel_code
FROM hotel_codes
ORDER BY hotel_code;
