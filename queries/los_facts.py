LOS_FACTS_SQL = """
WITH

/*
 * Current and LY intentionally start from separate source scans. Their
 * eligibility rules differ, and keeping each arrival predicate beside the
 * source relation makes the query's data flow explicit.
 */
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
            %(start_date)s::date::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            (%(end_date)s::date + 1)::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )
),

/*
 * LOS is isolated here. The active definition is the count of distinct
 * Stockholm room-night calendar dates. A date-derived implementation can
 * replace this CTE only after the standalone parity validation is reviewed.
 */
current_reservation_los AS (
    SELECT
        reservation_id,
        hotel_code,
        arrival_date,
        count(DISTINCT night_date)::int AS los
    FROM current_source
    GROUP BY
        reservation_id,
        hotel_code,
        arrival_date
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
    GROUP BY
        arrival_date,
        hotel_code,
        los
),

/*
 * Keep only LY rows that can still qualify for Actual LY or SPIT. Created and
 * cancelled values deliberately retain the existing ::date semantics.
 */
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
            (
                CASE
                    WHEN %(ly_comparison_basis)s = 'sameWeekday'
                        THEN %(start_date)s::date - 364
                    ELSE (%(start_date)s::date - INTERVAL '1 year')::date
                END
            )::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            (
                CASE
                    WHEN %(ly_comparison_basis)s = 'sameWeekday'
                        THEN %(end_date)s::date - 364 + 1
                    ELSE ((%(end_date)s::date - INTERVAL '1 year')::date + 1)
                END
            )::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
        AND (
            r.canceled_utc IS NULL
            OR (
                r.created_utc::date <= (
                    CASE
                        WHEN %(ly_comparison_basis)s = 'sameWeekday'
                            THEN CURRENT_DATE - 364
                        ELSE (CURRENT_DATE - INTERVAL '1 year')::date
                    END
                )
                AND r.canceled_utc::date > (
                    CASE
                        WHEN %(ly_comparison_basis)s = 'sameWeekday'
                            THEN CURRENT_DATE - 364
                        ELSE (CURRENT_DATE - INTERVAL '1 year')::date
                    END
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

/* Calculate LY reservation LOS once, then emit whichever scenarios qualify. */
ly_scenarios AS (
    SELECT
        CASE
            WHEN %(ly_comparison_basis)s = 'sameWeekday'
                THEN r.arrival_date + 364
            ELSE (r.arrival_date + INTERVAL '1 year')::date
        END AS arrival_date,
        r.hotel_code,
        scenario_data.scenario,
        r.los
    FROM ly_reservation_los r
    CROSS JOIN LATERAL (
        VALUES
            ('ly'::text, r.cancelled_date IS NULL),
            (
                'spit'::text,
                r.created_date <= (
                    CASE
                        WHEN %(ly_comparison_basis)s = 'sameWeekday'
                            THEN CURRENT_DATE - 364
                        ELSE (CURRENT_DATE - INTERVAL '1 year')::date
                    END
                )
                AND (
                    r.cancelled_date IS NULL
                    OR r.cancelled_date > (
                        CASE
                            WHEN %(ly_comparison_basis)s = 'sameWeekday'
                                THEN CURRENT_DATE - 364
                            ELSE (CURRENT_DATE - INTERVAL '1 year')::date
                        END
                    )
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
    GROUP BY
        arrival_date,
        hotel_code,
        scenario,
        los
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
"""
