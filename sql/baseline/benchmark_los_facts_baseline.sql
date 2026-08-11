/*
Pre-optimization LOS facts baseline. Run manually in pgAdmin before comparing
the candidate query. Each EXPLAIN ANALYZE executes live SQL; run only when the
database workload permits. Save every complete text plan separately.
*/

PREPARE los_facts_baseline(date, date, text) AS
WITH
current_source AS (
    SELECT
        r.number::text AS reservation_id,
        trim(r.hotel_name)::text AS hotel_code,
        (r.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS arrival_date,
        (r.night_start_utc AT TIME ZONE 'Europe/Stockholm')::date AS night_date
    FROM staging.room_nights_source r
    WHERE
        r.number IS NOT NULL
        AND r.hotel_name IS NOT NULL
        AND trim(r.hotel_name) <> ''
        AND r.start_utc IS NOT NULL
        AND r.night_start_utc IS NOT NULL
        AND r.canceled_utc IS NULL
        AND r.start_utc >= ($1::date::timestamp AT TIME ZONE 'Europe/Stockholm')
        AND r.start_utc < (($2::date + 1)::timestamp AT TIME ZONE 'Europe/Stockholm')
),
current_reservation_los AS (
    SELECT
        reservation_id,
        hotel_code,
        arrival_date,
        count(DISTINCT night_date)::int AS los
    FROM current_source
    GROUP BY reservation_id, hotel_code, arrival_date
),
current_facts AS (
    SELECT
        arrival_date,
        hotel_code,
        'current'::text AS scenario,
        los,
        count(*)::bigint AS booking_count,
        sum(los)::bigint AS night_count
    FROM current_reservation_los
    GROUP BY arrival_date, hotel_code, los
),
ly_source AS (
    SELECT
        r.number::text AS reservation_id,
        trim(r.hotel_name)::text AS hotel_code,
        (r.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS arrival_date,
        (r.night_start_utc AT TIME ZONE 'Europe/Stockholm')::date AS night_date,
        r.created_utc::date AS created_date,
        r.canceled_utc::date AS cancelled_date
    FROM staging.room_nights_source r
    WHERE
        r.number IS NOT NULL
        AND r.hotel_name IS NOT NULL
        AND trim(r.hotel_name) <> ''
        AND r.start_utc IS NOT NULL
        AND r.night_start_utc IS NOT NULL
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
        AND (
            r.canceled_utc IS NULL
            OR (
                r.created_utc::date <= CASE WHEN $3 = 'sameWeekday'
                    THEN CURRENT_DATE - 364 ELSE (CURRENT_DATE - INTERVAL '1 year')::date END
                AND r.canceled_utc::date > CASE WHEN $3 = 'sameWeekday'
                    THEN CURRENT_DATE - 364 ELSE (CURRENT_DATE - INTERVAL '1 year')::date END
            )
        )
),
ly_reservation_los AS (
    SELECT
        reservation_id,
        hotel_code,
        arrival_date,
        created_date,
        cancelled_date,
        count(DISTINCT night_date)::int AS los
    FROM ly_source
    GROUP BY reservation_id, hotel_code, arrival_date, created_date, cancelled_date
),
ly_scenarios AS (
    SELECT
        CASE WHEN $3 = 'sameWeekday' THEN r.arrival_date + 364
            ELSE (r.arrival_date + INTERVAL '1 year')::date END AS arrival_date,
        r.hotel_code,
        scenario_data.scenario,
        r.los
    FROM ly_reservation_los r
    CROSS JOIN LATERAL (
        VALUES
            ('ly'::text, r.cancelled_date IS NULL),
            ('spit'::text,
                r.created_date <= CASE WHEN $3 = 'sameWeekday'
                    THEN CURRENT_DATE - 364 ELSE (CURRENT_DATE - INTERVAL '1 year')::date END
                AND (
                    r.cancelled_date IS NULL
                    OR r.cancelled_date > CASE WHEN $3 = 'sameWeekday'
                        THEN CURRENT_DATE - 364 ELSE (CURRENT_DATE - INTERVAL '1 year')::date END
                )
            )
    ) AS scenario_data(scenario, include_reservation)
    WHERE scenario_data.include_reservation
),
ly_facts AS (
    SELECT
        arrival_date,
        hotel_code,
        scenario,
        los,
        count(*)::bigint AS booking_count,
        sum(los)::bigint AS night_count
    FROM ly_scenarios
    GROUP BY arrival_date, hotel_code, scenario, los
),
los_facts AS (
    SELECT * FROM current_facts
    UNION ALL
    SELECT * FROM ly_facts
)
SELECT arrival_date, hotel_code, scenario, los, booking_count, night_count
FROM los_facts
ORDER BY
    arrival_date,
    hotel_code,
    CASE scenario WHEN 'current' THEN 1 WHEN 'ly' THEN 2 WHEN 'spit' THEN 3 END,
    los;

/* One month / sameDate */
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_facts_baseline(DATE '2026-01-01', DATE '2026-01-31', 'sameDate');

/* One month / sameWeekday */
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_facts_baseline(DATE '2026-01-01', DATE '2026-01-31', 'sameWeekday');

/* Full year / sameDate */
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_facts_baseline(DATE '2026-01-01', DATE '2026-12-31', 'sameDate');

/* Full year / sameWeekday */
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_facts_baseline(DATE '2026-01-01', DATE '2026-12-31', 'sameWeekday');

DEALLOCATE los_facts_baseline;
