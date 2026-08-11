/*
Canonical LOS facts benchmark -- run manually in pgAdmin against production.

Representative request:
  startDate=2026-01-01
  endDate=2026-12-31
  lyComparisonBasis=sameWeekday

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

EXPLAIN (
    ANALYZE,
    BUFFERS,
    VERBOSE,
    SETTINGS,
    SUMMARY
)
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
        AND r.start_utc >= (
            DATE '2026-01-01'::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            DATE '2027-01-01'::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
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
            DATE '2025-01-02'::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            DATE '2026-01-02'::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
        AND (
            r.canceled_utc IS NULL
            OR (
                r.created_utc::date <= CURRENT_DATE - 364
                AND r.canceled_utc::date > CURRENT_DATE - 364
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
ly_scenarios AS (
    SELECT
        r.arrival_date + 364 AS arrival_date,
        r.hotel_code,
        scenario_data.scenario,
        r.los
    FROM ly_reservation_los r
    CROSS JOIN LATERAL (
        VALUES
            ('ly'::text, r.cancelled_date IS NULL),
            (
                'spit'::text,
                r.created_date <= CURRENT_DATE - 364
                AND (
                    r.cancelled_date IS NULL
                    OR r.cancelled_date > CURRENT_DATE - 364
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
SELECT
    arrival_date,
    hotel_code,
    scenario,
    los,
    booking_count,
    night_count
FROM los_facts
ORDER BY
    arrival_date,
    hotel_code,
    CASE scenario
        WHEN 'current' THEN 1
        WHEN 'ly' THEN 2
        WHEN 'spit' THEN 3
    END,
    los;
