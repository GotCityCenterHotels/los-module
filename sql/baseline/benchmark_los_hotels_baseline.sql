/*
Pre-optimization period-bounded hotel-list baseline. Run manually in pgAdmin
and save each complete plan separately. No expected execution time is stated.
*/

PREPARE los_hotels_baseline(date, date, text) AS
WITH hotel_codes AS (
    SELECT trim(r.hotel_name)::text AS hotel_code
    FROM staging.room_nights_source r
    WHERE
        r.hotel_name IS NOT NULL
        AND trim(r.hotel_name) <> ''
        AND r.start_utc >= ($1::date::timestamp AT TIME ZONE 'Europe/Stockholm')
        AND r.start_utc < (($2::date + 1)::timestamp AT TIME ZONE 'Europe/Stockholm')

    UNION ALL

    SELECT trim(r.hotel_name)::text AS hotel_code
    FROM staging.room_nights_source r
    WHERE
        r.hotel_name IS NOT NULL
        AND trim(r.hotel_name) <> ''
        AND r.start_utc >= (
            (CASE WHEN $3 = 'sameWeekday' THEN $1 - 364
                ELSE ($1 - INTERVAL '1 year')::date END)::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            (CASE WHEN $3 = 'sameWeekday' THEN $2 - 364 + 1
                ELSE (($2 - INTERVAL '1 year')::date + 1) END)::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )
)
SELECT DISTINCT hotel_code
FROM hotel_codes
ORDER BY hotel_code;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_hotels_baseline(DATE '2026-01-01', DATE '2026-01-31', 'sameDate');

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_hotels_baseline(DATE '2026-01-01', DATE '2026-01-31', 'sameWeekday');

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_hotels_baseline(DATE '2026-01-01', DATE '2026-12-31', 'sameDate');

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_hotels_baseline(DATE '2026-01-01', DATE '2026-12-31', 'sameWeekday');

DEALLOCATE los_hotels_baseline;
