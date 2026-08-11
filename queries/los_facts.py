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
        (los::bigint * count(*))::bigint AS night_count
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

/* Calculate eligibility once without expanding each reservation into rows. */
ly_reservation_flags AS (
    SELECT
        CASE
            WHEN %(ly_comparison_basis)s = 'sameWeekday'
                THEN r.arrival_date + 364
            ELSE (r.arrival_date + INTERVAL '1 year')::date
        END AS arrival_date,
        r.hotel_code,
        r.los,
        r.cancelled_date IS NULL AS include_ly,
        (
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
        ) AS include_spit
    FROM ly_reservation_los r
),

/* Aggregate first; scenario rows are emitted only at the small fact grain. */
ly_fact_components AS (
    SELECT
        arrival_date,
        hotel_code,
        los,
        count(*) FILTER (WHERE include_ly)::bigint AS ly_booking_count,
        count(*) FILTER (WHERE include_spit)::bigint AS spit_booking_count
    FROM ly_reservation_flags
    GROUP BY
        arrival_date,
        hotel_code,
        los
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
FROM los_facts
;
"""
