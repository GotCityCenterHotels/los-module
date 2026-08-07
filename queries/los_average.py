LOS_AVERAGE_SQL = """
WITH

/* ============================================================
   PARAMETERS
   ============================================================ */

params AS (
    SELECT
        %s::date AS start_date,
        %s::date AS end_date,
        %s::text AS grain,
        %s::text[] AS hotel_names,
        %s::text AS ly_comparison_basis
),


/* ============================================================
   COMPARISON DATES + UTC QUERY BOUNDARIES

   Calculate these once rather than repeatedly while scanning
   staging.room_nights_source.
   ============================================================ */

cutoffs AS (
    SELECT
        p.*,


        /* ----------------------------------------------------
           Historical booking-position cutoff
           ---------------------------------------------------- */

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


        /* ----------------------------------------------------
           LY requested date range
           ---------------------------------------------------- */

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


        /* ----------------------------------------------------
           Shift LY reservations onto comparable current date
           ---------------------------------------------------- */

        CASE
            WHEN p.ly_comparison_basis = 'sameWeekday'
                THEN INTERVAL '364 days'

            ELSE INTERVAL '1 year'
        END AS ly_forward_shift,


        /* ----------------------------------------------------
           Current UTC range
           ---------------------------------------------------- */

        (
            p.start_date::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        ) AS current_start_utc,


        (
            (
                p.end_date
                + 1
            )::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        ) AS current_end_utc

    FROM params p
),


/* ============================================================
   FINISH THE LY UTC BOUNDARIES

   Done separately because ly_start / ly_end were generated in
   the previous CTE.
   ============================================================ */

query_ranges AS (
    SELECT
        c.*,

        (
            c.ly_start::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        ) AS ly_start_utc,

        (
            (
                c.ly_end
                + 1
            )::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        ) AS ly_end_utc

    FROM cutoffs c
),


/* ============================================================
   SOURCE ROWS

   IMPORTANT OPTIMIZATION:

   Current and LY are separate SELECTs rather than:

       WHERE current_range OR ly_range

   This gives PostgreSQL two straightforward start_utc range
   scans and generally makes start_utc indexes easier to use.

   UNION ALL is intentional.

   If the requested ranges overlap, duplicate source rows do not
   change LOS because reservation_base later performs
   COUNT(DISTINCT night_date).
   ============================================================ */

source_rows AS (

    /* --------------------------------------------------------
       CURRENT RANGE
       -------------------------------------------------------- */

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


        /* Current arrival range */

        AND r.start_utc >=
            c.current_start_utc

        AND r.start_utc <
            c.current_end_utc


        /* Hotel filter */

        AND (
            c.hotel_names IS NULL

            OR trim(r.hotel_name) = ANY(
                c.hotel_names
            )
        )


        /*
         * Safe early cancellation filter.
         *
         * Anything cancelled on/before the historical cutoff
         * can belong to neither:
         *
         *   - current actual / LY actual
         *     because those require cancellation IS NULL
         *
         *   - SPIT
         *     because SPIT requires cancellation > cutoff
         *
         * Removing these rows before COUNT(DISTINCT) can reduce
         * the expensive aggregation substantially.
         */

        AND (
            r.canceled_utc IS NULL

            OR r.canceled_utc::date >
               c.created_cutoff
        )


    UNION ALL


    /* --------------------------------------------------------
       LAST-YEAR RANGE
       -------------------------------------------------------- */

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


        /* LY arrival range */

        AND r.start_utc >=
            c.ly_start_utc

        AND r.start_utc <
            c.ly_end_utc


        /* Hotel filter */

        AND (
            c.hotel_names IS NULL

            OR trim(r.hotel_name) = ANY(
                c.hotel_names
            )
        )


        /* Same safe early cancellation filter */

        AND (
            r.canceled_utc IS NULL

            OR r.canceled_utc::date >
               c.created_cutoff
        )
),


/* ============================================================
   ONE ROW PER RESERVATION

   This is the expensive reservation LOS calculation.

   It now happens ONCE.

   True LOS:
       count distinct calendar night dates

   We no longer construct:

       date | assigned_space_name

   so room moves / assigned spaces don't inflate LOS and
   PostgreSQL doesn't need to allocate/deduplicate strings.
   ============================================================ */

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


/* ============================================================
   BUILD CURRENT / LY / SPIT SCENARIOS

   Instead of three separate large CTE trees, each reservation
   is converted into the scenarios where it qualifies.

   A reservation can produce:
       current
       ly
       spit

   as appropriate.
   ============================================================ */

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


        /* ----------------------------------------------------
           CURRENT ACTUAL
           ---------------------------------------------------- */

        (
            'current'::text,

            r.arrival_date,

            (
                r.arrival_date
                    BETWEEN c.start_date
                    AND c.end_date

                AND r.cancelled_date
                    IS NULL
            )
        ),


        /* ----------------------------------------------------
           ACTUAL LY
           ---------------------------------------------------- */

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

                AND r.cancelled_date
                    IS NULL
            )
        ),


        /* ----------------------------------------------------
           SPIT
           ---------------------------------------------------- */

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

                    OR r.cancelled_date
                        IS NULL
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


/* ============================================================
   BUCKET RESERVATIONS

   Explicit CASE avoids relying on arbitrary date_trunc text.

   Python already validates:
       day
       week
       month
       year
   ============================================================ */

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

            WHEN p.grain = 'week'
                THEN date_trunc(
                    'week',
                    r.comparison_arrival_date
                )::date

            ELSE
                r.comparison_arrival_date

        END AS bucket_date,

        r.night_count

    FROM reservation_scenarios r

    CROSS JOIN params p
),


/* ============================================================
   HOTEL + TOTAL AGGREGATION

   GROUPING SETS creates both:

       bucket_date + hotel
       bucket_date + Total

   in the same aggregate operation.

   This replaces the old:

       los_agg
       los_total

       losly_agg
       losly_total

       spit_agg
       spit_total

   Also note:

       count(*)

   rather than:

       count(DISTINCT res_id)

   because bucketed_reservations is already one row per
   reservation/scenario.
   ============================================================ */

scenario_agg AS (
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


        /* Average reservation LOS */

        avg(
            b.night_count::numeric
        ) AS avg_los,


        /* Room nights */

        sum(
            b.night_count
        ) AS room_nights,


        /* Reservations */

        count(*) AS total_bookings


    FROM bucketed_reservations b


    GROUP BY
        b.scenario,

        GROUPING SETS (

            /* Hotel */

            (
                b.bucket_date,
                b.hotel_code
            ),

            /* Portfolio total */

            (
                b.bucket_date
            )

        )
),


/* ============================================================
   PIVOT THE THREE SCENARIOS

   scenario_agg is now tiny compared with the original
   room-night source table.
   ============================================================ */

final_result AS (
    SELECT
        a.bucket_date,

        a.hotel_code,


        /* ----------------------------------------------------
           LOS
           ---------------------------------------------------- */

        max(
            a.avg_los
        ) FILTER (
            WHERE
                a.scenario = 'current'
        ) AS los,


        max(
            a.avg_los
        ) FILTER (
            WHERE
                a.scenario = 'ly'
        ) AS losly,


        max(
            a.avg_los
        ) FILTER (
            WHERE
                a.scenario = 'spit'
        ) AS spit_los_non_strict_arrival,


        /* ----------------------------------------------------
           ROOM NIGHTS
           ---------------------------------------------------- */

        max(
            a.room_nights
        ) FILTER (
            WHERE
                a.scenario = 'current'
        ) AS rn,


        max(
            a.room_nights
        ) FILTER (
            WHERE
                a.scenario = 'ly'
        ) AS rnly,


        max(
            a.room_nights
        ) FILTER (
            WHERE
                a.scenario = 'spit'
        ) AS spit_rn_non_strict_arrival,


        /* ----------------------------------------------------
           BOOKINGS
           ---------------------------------------------------- */

        max(
            a.total_bookings
        ) FILTER (
            WHERE
                a.scenario = 'current'
        ) AS total_bookings,


        max(
            a.total_bookings
        ) FILTER (
            WHERE
                a.scenario = 'ly'
        ) AS total_bookings_ly,


        max(
            a.total_bookings
        ) FILTER (
            WHERE
                a.scenario = 'spit'
        ) AS total_bookings_spit


    FROM scenario_agg a


    GROUP BY
        a.bucket_date,
        a.hotel_code
)


/* ============================================================
   FINAL OUTPUT
   ============================================================ */

SELECT
    bucket_date,

    hotel_code,

    los,

    losly,

    spit_los_non_strict_arrival,

    rn,

    rnly,

    spit_rn_non_strict_arrival,

    total_bookings,

    total_bookings_ly,

    total_bookings_spit

FROM final_result

ORDER BY
    bucket_date,

    CASE
        WHEN hotel_code = 'Total'
            THEN 1
        ELSE 0
    END,

    hotel_code;
"""
