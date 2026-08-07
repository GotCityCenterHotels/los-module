LOS_DISTRIBUTION_SQL = """
WITH

params AS (
    SELECT
        %s::date AS start_date,
        %s::date AS end_date,
        %s::text AS grain,
        %s::text AS hotel_name,
        %s::text AS ly_comparison_basis
),

cutoffs AS (
    SELECT
        p.*,

        CASE
            WHEN p.ly_comparison_basis = 'sameWeekday'
                THEN (
                    CURRENT_DATE
                    - INTERVAL '364 days'
                )::date
            ELSE (
                CURRENT_DATE
                - INTERVAL '1 year'
            )::date
        END AS created_cutoff,

        CASE
            WHEN p.ly_comparison_basis = 'sameWeekday'
                THEN (
                    p.start_date
                    - INTERVAL '364 days'
                )::date
            ELSE (
                p.start_date
                - INTERVAL '1 year'
            )::date
        END AS ly_start,

        CASE
            WHEN p.ly_comparison_basis = 'sameWeekday'
                THEN (
                    p.end_date
                    - INTERVAL '364 days'
                )::date
            ELSE (
                p.end_date
                - INTERVAL '1 year'
            )::date
        END AS ly_end,

        CASE
            WHEN p.ly_comparison_basis = 'sameWeekday'
                THEN INTERVAL '364 days'
            ELSE INTERVAL '1 year'
        END AS ly_forward_shift,

        (
            p.start_date::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        ) AS current_start_utc,

        (
            (p.end_date + 1)::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        ) AS current_end_utc

    FROM params p
),

query_ranges AS (
    SELECT
        c.*,

        (
            c.ly_start::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        ) AS ly_start_utc,

        (
            (c.ly_end + 1)::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        ) AS ly_end_utc

    FROM cutoffs c
),

/* ==========================================================
   ROOM NIGHT SOURCE ROWS
   ========================================================== */

source_rows AS (

    /* Current period */

    SELECT
        r.number::text AS res_id,

        trim(
            r.hotel_name
        )::text AS hotel_code,

        (
            r.night_start_utc
            AT TIME ZONE 'Europe/Stockholm'
        )::date AS night_date,

        (
            r.start_utc
            AT TIME ZONE 'Europe/Stockholm'
        )::date AS arrival_date,

        r.created_utc::date AS created_date,

        r.canceled_utc::date AS cancelled_date

    FROM staging.room_nights_source r

    CROSS JOIN query_ranges c

    WHERE
        r.number IS NOT NULL

        AND r.start_utc IS NOT NULL

        AND r.night_start_utc IS NOT NULL

        AND r.start_utc >=
            c.current_start_utc

        AND r.start_utc <
            c.current_end_utc

        AND (
            c.hotel_name IS NULL
            OR trim(r.hotel_name) =
               c.hotel_name
        )

        AND (
            r.canceled_utc IS NULL
            OR r.canceled_utc::date >
               c.created_cutoff
        )


    UNION ALL


    /* Last-year period */

    SELECT
        r.number::text AS res_id,

        trim(
            r.hotel_name
        )::text AS hotel_code,

        (
            r.night_start_utc
            AT TIME ZONE 'Europe/Stockholm'
        )::date AS night_date,

        (
            r.start_utc
            AT TIME ZONE 'Europe/Stockholm'
        )::date AS arrival_date,

        r.created_utc::date AS created_date,

        r.canceled_utc::date AS cancelled_date

    FROM staging.room_nights_source r

    CROSS JOIN query_ranges c

    WHERE
        r.number IS NOT NULL

        AND r.start_utc IS NOT NULL

        AND r.night_start_utc IS NOT NULL

        AND r.start_utc >=
            c.ly_start_utc

        AND r.start_utc <
            c.ly_end_utc

        AND (
            c.hotel_name IS NULL
            OR trim(r.hotel_name) =
               c.hotel_name
        )

        AND (
            r.canceled_utc IS NULL
            OR r.canceled_utc::date >
               c.created_cutoff
        )
),

/* ==========================================================
   ONE ROW PER RESERVATION

   True LOS = distinct calendar stay dates.
   ========================================================== */

reservation_base AS (
    SELECT
        s.res_id,

        s.hotel_code,

        s.arrival_date,

        s.created_date,

        s.cancelled_date,

        count(
            DISTINCT s.night_date
        )::int AS night_count

    FROM source_rows s

    GROUP BY
        s.res_id,
        s.hotel_code,
        s.arrival_date,
        s.created_date,
        s.cancelled_date
),

/* ==========================================================
   CURRENT / LY / SPIT
   ========================================================== */

reservation_scenarios AS (
    SELECT
        scenario_data.scenario,

        r.res_id,

        r.hotel_code,

        scenario_data.comparison_arrival_date,

        r.night_count

    FROM reservation_base r

    CROSS JOIN query_ranges c

    CROSS JOIN LATERAL (
        VALUES

        /* Current */

        (
            'current'::text,

            r.arrival_date,

            (
                r.arrival_date
                    BETWEEN c.start_date
                    AND c.end_date

                AND r.cancelled_date IS NULL
            )
        ),

        /* Actual LY */

        (
            'ly'::text,

            (
                r.arrival_date
                + c.ly_forward_shift
            )::date,

            (
                r.arrival_date
                    BETWEEN c.ly_start
                    AND c.ly_end

                AND r.cancelled_date IS NULL
            )
        ),

        /* SPIT */

        (
            'spit'::text,

            (
                r.arrival_date
                + c.ly_forward_shift
            )::date,

            (
                r.arrival_date
                    BETWEEN c.ly_start
                    AND c.ly_end

                AND r.created_date <=
                    c.created_cutoff

                AND (
                    r.cancelled_date >
                        c.created_cutoff

                    OR r.cancelled_date IS NULL
                )
            )
        )

    ) AS scenario_data(
        scenario,
        comparison_arrival_date,
        include_row
    )

    WHERE
        scenario_data.include_row
),

/* ==========================================================
   DAY / MONTH / YEAR
   ========================================================== */

bucketed_reservations AS (
    SELECT
        r.scenario,

        r.res_id,

        r.hotel_code,

        CASE
            WHEN p.grain = 'year'
                THEN date_trunc(
                    'year',
                    r.comparison_arrival_date
                )::date

            WHEN p.grain = 'month'
                THEN date_trunc(
                    'month',
                    r.comparison_arrival_date
                )::date

            ELSE
                r.comparison_arrival_date

        END AS bucket_date,

        r.night_count

    FROM reservation_scenarios r

    CROSS JOIN params p
),

/* ==========================================================
   DISTRIBUTION

   Calculate BOOKING distribution and NIGHT distribution
   simultaneously.

   A 7-night reservation contributes:

       bookings LOS 5+ = 1
       nights LOS 5+   = 7
   ========================================================== */

distribution AS (
    SELECT
        b.scenario,

        b.bucket_date,

        CASE
            WHEN GROUPING(
                b.hotel_code
            ) = 1
                THEN 'Total'::text

            ELSE b.hotel_code
        END AS hotel_code,


        /* ================================================
           BOOKING DISTRIBUTION
           ================================================ */

        count(*)::bigint
            AS total_bookings,


        count(*) FILTER (
            WHERE b.night_count = 1
        )::bigint
            AS los_1_bookings,


        count(*) FILTER (
            WHERE b.night_count = 2
        )::bigint
            AS los_2_bookings,


        count(*) FILTER (
            WHERE b.night_count = 3
        )::bigint
            AS los_3_bookings,


        count(*) FILTER (
            WHERE b.night_count = 4
        )::bigint
            AS los_4_bookings,


        count(*) FILTER (
            WHERE b.night_count >= 5
        )::bigint
            AS los_5_plus_bookings,


        /* ================================================
           NIGHT DISTRIBUTION
           ================================================ */

        sum(
            b.night_count
        )::bigint
            AS total_nights,


        coalesce(
            sum(
                b.night_count
            ) FILTER (
                WHERE b.night_count = 1
            ),
            0
        )::bigint
            AS los_1_nights,


        coalesce(
            sum(
                b.night_count
            ) FILTER (
                WHERE b.night_count = 2
            ),
            0
        )::bigint
            AS los_2_nights,


        coalesce(
            sum(
                b.night_count
            ) FILTER (
                WHERE b.night_count = 3
            ),
            0
        )::bigint
            AS los_3_nights,


        coalesce(
            sum(
                b.night_count
            ) FILTER (
                WHERE b.night_count = 4
            ),
            0
        )::bigint
            AS los_4_nights,


        coalesce(
            sum(
                b.night_count
            ) FILTER (
                WHERE b.night_count >= 5
            ),
            0
        )::bigint
            AS los_5_plus_nights


    FROM bucketed_reservations b

    GROUP BY
        b.scenario,

        GROUPING SETS (
            (
                b.bucket_date,
                b.hotel_code
            ),
            (
                b.bucket_date
            )
        )
)

/* ==========================================================
   FINAL API RESULT
   ========================================================== */

SELECT
    bucket_date,

    hotel_code,

    scenario,

    total_bookings,

    los_1_bookings,

    los_2_bookings,

    los_3_bookings,

    los_4_bookings,

    los_5_plus_bookings,

    total_nights,

    los_1_nights,

    los_2_nights,

    los_3_nights,

    los_4_nights,

    los_5_plus_nights

FROM distribution

ORDER BY
    bucket_date,

    CASE
        WHEN hotel_code = 'Total'
            THEN 1
        ELSE 0
    END,

    hotel_code,

    CASE scenario
        WHEN 'current'
            THEN 1

        WHEN 'ly'
            THEN 2

        WHEN 'spit'
            THEN 3
    END;
"""