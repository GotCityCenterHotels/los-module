/*
Optimized canonical LOS facts candidate -- run manually in pgAdmin after the
matching baseline script. The four cases cover one month/full year and both
comparison bases.

Inspect, without assuming a particular result:
  - Execution Time, planning time, shared hits/reads, and temp reads/writes.
  - The scans beneath staging.room_nights_source. In the expanded view plan,
    locate reservation_current and any order_item_current involvement.
  - Whether both Current and LY start_utc ranges appear as scan/index
    conditions early, and whether reservation_current_start_utc_idx is chosen.
  - Rows removed by filters and whether cancellation predicates are early.
  - Actual rows/loops on nested loops and unexpectedly repeated inner work.
  - Heap fetches for index-only scans and large shared read counts.
  - Sort Method lines, especially external disk sorts, plus temp blocks.
  - Runtime attributable to the Current branch versus the LY branch.
  - The distinct night-date aggregates and whether LY LOS is computed once
    before branching into Actual LY and SPIT.

Do not compare only the final Execution Time. Save the complete text plan and
send it back for analysis. This script contains no expected timing.
*/

PREPARE los_facts_candidate(date, date, text) AS
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
        (los::bigint * count(*))::bigint AS night_count
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
                r.created_utc::date <= (
                    CASE WHEN $3 = 'sameWeekday' THEN CURRENT_DATE - 364
                        ELSE (CURRENT_DATE - INTERVAL '1 year')::date END
                )
                AND r.canceled_utc::date > (
                    CASE WHEN $3 = 'sameWeekday' THEN CURRENT_DATE - 364
                        ELSE (CURRENT_DATE - INTERVAL '1 year')::date END
                )
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
    GROUP BY
        reservation_id,
        hotel_code,
        arrival_date,
        created_date,
        cancelled_date
),
ly_reservation_flags AS (
    SELECT
        CASE WHEN $3 = 'sameWeekday' THEN r.arrival_date + 364
            ELSE (r.arrival_date + INTERVAL '1 year')::date END AS arrival_date,
        r.hotel_code,
        r.los,
        r.cancelled_date IS NULL AS include_ly,
        r.created_date <= (
            CASE WHEN $3 = 'sameWeekday' THEN CURRENT_DATE - 364
                ELSE (CURRENT_DATE - INTERVAL '1 year')::date END
        ) AND (
            r.cancelled_date IS NULL
            OR r.cancelled_date > (
                CASE WHEN $3 = 'sameWeekday' THEN CURRENT_DATE - 364
                    ELSE (CURRENT_DATE - INTERVAL '1 year')::date END
            )
        ) AS include_spit
    FROM ly_reservation_los r
),
ly_fact_components AS (
    SELECT
        arrival_date,
        hotel_code,
        los,
        count(*) FILTER (WHERE include_ly)::bigint AS ly_booking_count,
        count(*) FILTER (WHERE include_spit)::bigint AS spit_booking_count
    FROM ly_reservation_flags
    GROUP BY arrival_date, hotel_code, los
),
ly_facts AS (
    SELECT
        f.arrival_date,
        f.hotel_code,
        scenario_data.scenario,
        f.los,
        scenario_data.booking_count,
        (f.los::bigint * scenario_data.booking_count)::bigint AS night_count
    FROM ly_fact_components f
    CROSS JOIN LATERAL (
        VALUES
            ('ly'::text, f.ly_booking_count),
            ('spit'::text, f.spit_booking_count)
    ) AS scenario_data(scenario, booking_count)
    WHERE scenario_data.booking_count > 0
),
los_facts AS (
    SELECT * FROM current_facts
    UNION ALL
    SELECT * FROM ly_facts
)
SELECT
    arrival_date,
    hotel_code,
    scenario,
    los,
    booking_count,
    night_count
FROM los_facts;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_facts_candidate(DATE '2026-01-01', DATE '2026-01-31', 'sameDate');

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_facts_candidate(DATE '2026-01-01', DATE '2026-01-31', 'sameWeekday');

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_facts_candidate(DATE '2026-01-01', DATE '2026-12-31', 'sameDate');

EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, SUMMARY)
EXECUTE los_facts_candidate(DATE '2026-01-01', DATE '2026-12-31', 'sameWeekday');

DEALLOCATE los_facts_candidate;
