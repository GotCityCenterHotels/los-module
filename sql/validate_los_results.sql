/*
SUPERSEDED for deployment parity: use validate_los_read_model.py, which
compares the deployed raw query with the current Database A publication.
This script remains useful for exploratory frontend-grain checks.

Canonical-output correctness checks -- run manually in pgAdmin.

Change only the VALUES row to exercise the validation matrix:
  1. one month, hotel_code NULL (portfolio), sameDate
  2. one month, one exact hotel_code, sameDate
  3. full year, hotel_code NULL, sameDate
  4. full year, one exact hotel_code, sameDate
  5. repeat cases 1-4 with sameWeekday

Before deploying the redesign, export the old /api/los/average and
/api/los/distribution responses for each case. The result sets at the bottom
are shaped for field-by-field comparison with those exports. This script does
not claim production parity; save any differences and the relevant detail rows.
*/

DROP TABLE IF EXISTS pg_temp.los_validation_parameters;
DROP TABLE IF EXISTS pg_temp.los_facts_validation;

CREATE TEMP TABLE los_validation_parameters (
    start_date date NOT NULL,
    end_date date NOT NULL,
    ly_comparison_basis text NOT NULL,
    hotel_code text NULL,
    CHECK (start_date <= end_date),
    CHECK (ly_comparison_basis IN ('sameDate', 'sameWeekday'))
);

INSERT INTO los_validation_parameters VALUES
    (DATE '2026-01-01', DATE '2026-12-31', 'sameWeekday', NULL);

CREATE TEMP TABLE los_facts_validation AS
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
            (SELECT start_date FROM los_validation_parameters)::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            ((SELECT end_date FROM los_validation_parameters) + 1)::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )
        AND (
            (SELECT hotel_code FROM los_validation_parameters) IS NULL
            OR trim(r.hotel_name) = (
                SELECT hotel_code FROM los_validation_parameters
            )
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
            (
                CASE
                    WHEN (SELECT ly_comparison_basis FROM los_validation_parameters) = 'sameWeekday'
                        THEN (SELECT start_date FROM los_validation_parameters) - 364
                    ELSE ((SELECT start_date FROM los_validation_parameters) - INTERVAL '1 year')::date
                END
            )::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            (
                CASE
                    WHEN (SELECT ly_comparison_basis FROM los_validation_parameters) = 'sameWeekday'
                        THEN (SELECT end_date FROM los_validation_parameters) - 364 + 1
                    ELSE (((SELECT end_date FROM los_validation_parameters) - INTERVAL '1 year')::date + 1)
                END
            )::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
        AND (
            (SELECT hotel_code FROM los_validation_parameters) IS NULL
            OR trim(r.hotel_name) = (
                SELECT hotel_code FROM los_validation_parameters
            )
        )
        AND (
            r.canceled_utc IS NULL
            OR (
                r.created_utc::date <= (
                    CASE
                        WHEN (SELECT ly_comparison_basis FROM los_validation_parameters) = 'sameWeekday'
                            THEN CURRENT_DATE - 364
                        ELSE (CURRENT_DATE - INTERVAL '1 year')::date
                    END
                )
                AND r.canceled_utc::date > (
                    CASE
                        WHEN (SELECT ly_comparison_basis FROM los_validation_parameters) = 'sameWeekday'
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
ly_scenarios AS (
    SELECT
        CASE
            WHEN (SELECT ly_comparison_basis FROM los_validation_parameters) = 'sameWeekday'
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
                        WHEN (SELECT ly_comparison_basis FROM los_validation_parameters) = 'sameWeekday'
                            THEN CURRENT_DATE - 364
                        ELSE (CURRENT_DATE - INTERVAL '1 year')::date
                    END
                )
                AND (
                    r.cancelled_date IS NULL
                    OR r.cancelled_date > (
                        CASE
                            WHEN (SELECT ly_comparison_basis FROM los_validation_parameters) = 'sameWeekday'
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
    GROUP BY arrival_date, hotel_code, scenario, los
)
SELECT * FROM current_facts
UNION ALL
SELECT * FROM ly_facts;

/* 1. Fact-grain uniqueness and additive invariants: both should return 0. */
SELECT count(*) AS duplicate_fact_keys
FROM (
    SELECT arrival_date, hotel_code, scenario, los
    FROM los_facts_validation
    GROUP BY arrival_date, hotel_code, scenario, los
    HAVING count(*) > 1
) duplicates;

SELECT
    count(*) FILTER (WHERE booking_count < 0 OR night_count < 0) AS negative_rows,
    count(*) FILTER (WHERE night_count <> los * booking_count) AS non_additive_rows
FROM los_facts_validation;

/* 2. Required scenario totals and Average LOS from additive components. */
SELECT
    scenario,
    sum(booking_count)::bigint AS booking_count,
    sum(night_count)::bigint AS room_nights,
    sum(night_count)::numeric / nullif(sum(booking_count), 0) AS average_los
FROM los_facts_validation
GROUP BY scenario
ORDER BY CASE scenario WHEN 'current' THEN 1 WHEN 'ly' THEN 2 ELSE 3 END;

/* 3. Distribution by exact LOS. */
SELECT
    scenario,
    hotel_code,
    los,
    sum(booking_count)::bigint AS booking_count,
    sum(night_count)::bigint AS room_nights
FROM los_facts_validation
GROUP BY scenario, hotel_code, los
ORDER BY scenario, hotel_code, los;

/* 4. Frontend-style 1 / 2 / 3 / 4 / 5+ distribution and percentages. */
WITH bucketed AS (
    SELECT
        scenario,
        hotel_code,
        CASE WHEN los >= 5 THEN '5+' ELSE los::text END AS los_bucket,
        sum(booking_count)::bigint AS booking_count,
        sum(night_count)::bigint AS room_nights
    FROM los_facts_validation
    GROUP BY
        scenario,
        hotel_code,
        CASE WHEN los >= 5 THEN '5+' ELSE los::text END
)
SELECT
    scenario,
    hotel_code,
    los_bucket,
    booking_count,
    room_nights,
    100.0 * booking_count / nullif(sum(booking_count) OVER (PARTITION BY scenario, hotel_code), 0)
        AS booking_percentage,
    100.0 * room_nights / nullif(sum(room_nights) OVER (PARTITION BY scenario, hotel_code), 0)
        AS room_night_percentage
FROM bucketed
ORDER BY scenario, hotel_code, CASE los_bucket WHEN '5+' THEN 5 ELSE los_bucket::int END;

/*
5. Legacy-average-shaped monthly output, including frontend-derived Total.
Compare LOS/rn/booking fields with the saved old Average endpoint response.
*/
WITH monthly AS (
    SELECT
        date_trunc('month', arrival_date)::date AS bucket_date,
        CASE WHEN GROUPING(hotel_code) = 1 THEN 'Total' ELSE hotel_code END AS hotel_code,
        scenario,
        sum(booking_count)::bigint AS bookings,
        sum(night_count)::bigint AS nights
    FROM los_facts_validation
    GROUP BY scenario, GROUPING SETS (
        (date_trunc('month', arrival_date)::date, hotel_code),
        (date_trunc('month', arrival_date)::date)
    )
)
SELECT
    bucket_date,
    hotel_code,
    max(nights::numeric / nullif(bookings, 0)) FILTER (WHERE scenario = 'current') AS los,
    max(nights::numeric / nullif(bookings, 0)) FILTER (WHERE scenario = 'ly') AS losly,
    max(nights::numeric / nullif(bookings, 0)) FILTER (WHERE scenario = 'spit') AS spit_los_non_strict_arrival,
    max(nights) FILTER (WHERE scenario = 'current') AS rn,
    max(nights) FILTER (WHERE scenario = 'ly') AS rnly,
    max(nights) FILTER (WHERE scenario = 'spit') AS spit_rn_non_strict_arrival,
    max(bookings) FILTER (WHERE scenario = 'current') AS total_bookings,
    max(bookings) FILTER (WHERE scenario = 'ly') AS total_bookings_ly,
    max(bookings) FILTER (WHERE scenario = 'spit') AS total_bookings_spit
FROM monthly
GROUP BY bucket_date, hotel_code
ORDER BY bucket_date, hotel_code;

/*
6. Daily/monthly/yearly reconciliation. Each scenario's counts must match at
every grain. This also checks that portfolio Total is the sum of hotels.
*/
WITH grain_totals AS (
    SELECT 'day' AS grain, scenario, sum(booking_count) AS bookings, sum(night_count) AS nights
    FROM los_facts_validation GROUP BY scenario
    UNION ALL
    SELECT 'month', scenario, sum(bookings), sum(nights)
    FROM (
        SELECT scenario, date_trunc('month', arrival_date),
            sum(booking_count) AS bookings, sum(night_count) AS nights
        FROM los_facts_validation GROUP BY scenario, date_trunc('month', arrival_date)
    ) month_rows GROUP BY scenario
    UNION ALL
    SELECT 'year', scenario, sum(bookings), sum(nights)
    FROM (
        SELECT scenario, date_trunc('year', arrival_date),
            sum(booking_count) AS bookings, sum(night_count) AS nights
        FROM los_facts_validation GROUP BY scenario, date_trunc('year', arrival_date)
    ) year_rows GROUP BY scenario
)
SELECT * FROM grain_totals
ORDER BY scenario, CASE grain WHEN 'day' THEN 1 WHEN 'month' THEN 2 ELSE 3 END;
