LOS_AVERAGE_SQL = """
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
        THEN (now()::date - interval '364 days')::date
      ELSE (now()::date - interval '1 year')::date
    END AS created_cutoff,

    CASE
      WHEN p.ly_comparison_basis = 'sameWeekday'
        THEN (p.start_date - interval '364 days')::date
      ELSE (p.start_date - interval '1 year')::date
    END AS ly_start,

    CASE
      WHEN p.ly_comparison_basis = 'sameWeekday'
        THEN (p.end_date - interval '364 days')::date
      ELSE (p.end_date - interval '1 year')::date
    END AS ly_end

  FROM params p
),

/* ============================================================
   RAW ROOM NIGHT ROWS
   ============================================================ */

room_night_rows AS (
  SELECT
    r.number::text AS res_id,

    trim(r.hotel_name)::text AS hotel_code,

    concat_ws(
      '|',
      (
        r.night_start_utc
        AT TIME ZONE 'Europe/Stockholm'
      )::date::text,
      coalesce(
        trim(r.assigned_space_name),
        ''
      )
    ) AS night_key,

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

  CROSS JOIN cutoffs c

  WHERE r.number IS NOT NULL

    AND r.start_utc IS NOT NULL

    AND r.night_start_utc IS NOT NULL

    /*
      Load both:
      - selected current period
      - required last-year comparison period
    */
    AND (
      (
        r.start_utc >=
          c.start_date
          AT TIME ZONE 'Europe/Stockholm'

        AND r.start_utc <
          (
            c.end_date
            + interval '1 day'
          )
          AT TIME ZONE 'Europe/Stockholm'
      )

      OR

      (
        r.start_utc >=
          c.ly_start
          AT TIME ZONE 'Europe/Stockholm'

        AND r.start_utc <
          (
            c.ly_end
            + interval '1 day'
          )
          AT TIME ZONE 'Europe/Stockholm'
      )
    )

    /*
      NULL hotel_name means all hotels
    */
    AND (
      c.hotel_name IS NULL
      OR trim(r.hotel_name) = c.hotel_name
    )
),

/* ============================================================
   CURRENT RESERVATIONS
   ============================================================ */

los_base AS (
  SELECT
    r.res_id,

    r.hotel_code,

    /*
      Original LOS definition from your query.

      One reservation/space/date combination counts once.
    */
    count(
      DISTINCT r.night_key
    )::int AS night_count,

    CASE
      WHEN p.grain = 'year'
        THEN date_trunc(
          'year',
          r.arrival_date
        )::date

      WHEN p.grain = 'month'
        THEN date_trunc(
          'month',
          r.arrival_date
        )::date

      ELSE r.arrival_date

    END AS bucket_date

  FROM room_night_rows r

  CROSS JOIN params p

  WHERE
    r.arrival_date
      BETWEEN p.start_date
      AND p.end_date

    AND r.cancelled_date IS NULL

  GROUP BY
    r.res_id,
    r.hotel_code,
    bucket_date
),

/* ============================================================
   ACTUAL LAST YEAR
   ============================================================ */

losly_base AS (
  SELECT
    r.res_id,

    r.hotel_code,

    count(
      DISTINCT r.night_key
    )::int AS night_count,

    /*
      First shift LY arrival onto the comparable current
      period date.

      sameDate    = + 1 year
      sameWeekday = + 364 days

      Then apply day/month/year grain.
    */
    CASE
      WHEN c.grain = 'year'
        THEN date_trunc(
          'year',

          CASE
            WHEN c.ly_comparison_basis = 'sameWeekday'
              THEN (
                r.arrival_date
                + interval '364 days'
              )

            ELSE (
              r.arrival_date
              + interval '1 year'
            )
          END
        )::date

      WHEN c.grain = 'month'
        THEN date_trunc(
          'month',

          CASE
            WHEN c.ly_comparison_basis = 'sameWeekday'
              THEN (
                r.arrival_date
                + interval '364 days'
              )

            ELSE (
              r.arrival_date
              + interval '1 year'
            )
          END
        )::date

      ELSE
        (
          CASE
            WHEN c.ly_comparison_basis = 'sameWeekday'
              THEN (
                r.arrival_date
                + interval '364 days'
              )

            ELSE (
              r.arrival_date
              + interval '1 year'
            )
          END
        )::date

    END AS bucket_date

  FROM room_night_rows r

  CROSS JOIN cutoffs c

  WHERE
    r.arrival_date
      BETWEEN c.ly_start
      AND c.ly_end

    AND r.cancelled_date IS NULL

  GROUP BY
    r.res_id,
    r.hotel_code,
    bucket_date
),

/* ============================================================
   SPIT
   Last year's booking position at the equivalent cutoff
   ============================================================ */

spit_base AS (
  SELECT
    r.res_id,

    r.hotel_code,

    count(
      DISTINCT r.night_key
    )::int AS night_count,

    CASE
      WHEN c.grain = 'year'
        THEN date_trunc(
          'year',

          CASE
            WHEN c.ly_comparison_basis = 'sameWeekday'
              THEN (
                r.arrival_date
                + interval '364 days'
              )

            ELSE (
              r.arrival_date
              + interval '1 year'
            )
          END
        )::date

      WHEN c.grain = 'month'
        THEN date_trunc(
          'month',

          CASE
            WHEN c.ly_comparison_basis = 'sameWeekday'
              THEN (
                r.arrival_date
                + interval '364 days'
              )

            ELSE (
              r.arrival_date
              + interval '1 year'
            )
          END
        )::date

      ELSE
        (
          CASE
            WHEN c.ly_comparison_basis = 'sameWeekday'
              THEN (
                r.arrival_date
                + interval '364 days'
              )

            ELSE (
              r.arrival_date
              + interval '1 year'
            )
          END
        )::date

    END AS bucket_date

  FROM room_night_rows r

  CROSS JOIN cutoffs c

  WHERE
    r.created_date <= c.created_cutoff

    AND r.arrival_date
      BETWEEN c.ly_start
      AND c.ly_end

    /*
      Reservation must have been active at the
      historical SPIT cutoff.
    */
    AND (
      r.cancelled_date > c.created_cutoff
      OR r.cancelled_date IS NULL
    )

  GROUP BY
    r.res_id,
    r.hotel_code,
    bucket_date
),

/* ============================================================
   CURRENT HOTEL AGGREGATION
   ============================================================ */

los_agg AS (
  SELECT
    bucket_date,

    hotel_code,

    avg(
      night_count::numeric
    ) AS los,

    sum(
      night_count
    ) AS rn,

    count(
      DISTINCT res_id
    ) AS total_bookings

  FROM los_base

  GROUP BY
    bucket_date,
    hotel_code
),

/* ============================================================
   ACTUAL LY HOTEL AGGREGATION
   ============================================================ */

losly_agg AS (
  SELECT
    bucket_date,

    hotel_code,

    avg(
      night_count::numeric
    ) AS losly,

    sum(
      night_count
    ) AS rnly,

    count(
      DISTINCT res_id
    ) AS total_bookings_ly

  FROM losly_base

  GROUP BY
    bucket_date,
    hotel_code
),

/* ============================================================
   SPIT HOTEL AGGREGATION
   ============================================================ */

spit_agg AS (
  SELECT
    bucket_date,

    hotel_code,

    avg(
      night_count::numeric
    ) AS spit_los_non_strict_arrival,

    sum(
      night_count
    ) AS spit_rn_non_strict_arrival,

    count(
      DISTINCT res_id
    ) AS total_bookings_spit

  FROM spit_base

  GROUP BY
    bucket_date,
    hotel_code
),

/* ============================================================
   COMBINE HOTEL LEVEL
   ============================================================ */

per_hotel AS (
  SELECT
    coalesce(
      a.bucket_date,
      ly.bucket_date,
      s.bucket_date
    ) AS bucket_date,

    coalesce(
      a.hotel_code,
      ly.hotel_code,
      s.hotel_code
    ) AS hotel_code,

    a.los,

    ly.losly,

    s.spit_los_non_strict_arrival,

    a.rn,

    ly.rnly,

    s.spit_rn_non_strict_arrival,

    a.total_bookings,

    ly.total_bookings_ly,

    s.total_bookings_spit

  FROM los_agg a

  FULL JOIN losly_agg ly

    ON ly.bucket_date = a.bucket_date

    AND ly.hotel_code = a.hotel_code

  FULL JOIN spit_agg s

    ON s.bucket_date = coalesce(
      a.bucket_date,
      ly.bucket_date
    )

    AND s.hotel_code = coalesce(
      a.hotel_code,
      ly.hotel_code
    )
),

/* ============================================================
   CURRENT TOTAL
   ============================================================ */

los_total AS (
  SELECT
    bucket_date,

    /*
      Calculate from reservation-level rows rather than
      averaging hotel averages.
    */
    avg(
      night_count::numeric
    ) AS los,

    sum(
      night_count
    ) AS rn,

    count(
      DISTINCT res_id
    ) AS total_bookings

  FROM los_base

  GROUP BY
    bucket_date
),

/* ============================================================
   ACTUAL LY TOTAL
   ============================================================ */

losly_total AS (
  SELECT
    bucket_date,

    avg(
      night_count::numeric
    ) AS losly,

    sum(
      night_count
    ) AS rnly,

    count(
      DISTINCT res_id
    ) AS total_bookings_ly

  FROM losly_base

  GROUP BY
    bucket_date
),

/* ============================================================
   SPIT TOTAL
   ============================================================ */

spit_total AS (
  SELECT
    bucket_date,

    avg(
      night_count::numeric
    ) AS spit_los_non_strict_arrival,

    sum(
      night_count
    ) AS spit_rn_non_strict_arrival,

    count(
      DISTINCT res_id
    ) AS total_bookings_spit

  FROM spit_base

  GROUP BY
    bucket_date
),

/* ============================================================
   COMBINED TOTAL ROWS
   ============================================================ */

total_rows AS (
  SELECT
    coalesce(
      t.bucket_date,
      ly.bucket_date,
      s.bucket_date
    ) AS bucket_date,

    'Total'::text AS hotel_code,

    t.los,

    ly.losly,

    s.spit_los_non_strict_arrival,

    t.rn,

    ly.rnly,

    s.spit_rn_non_strict_arrival,

    t.total_bookings,

    ly.total_bookings_ly,

    s.total_bookings_spit

  FROM los_total t

  FULL JOIN losly_total ly

    ON ly.bucket_date = t.bucket_date

  FULL JOIN spit_total s

    ON s.bucket_date = coalesce(
      t.bucket_date,
      ly.bucket_date
    )
)

/* ============================================================
   FINAL API RESULT
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

FROM (
  SELECT
    *
  FROM per_hotel

  UNION ALL

  SELECT
    *
  FROM total_rows

) x

ORDER BY
  bucket_date,

  CASE
    WHEN hotel_code = 'Total'
      THEN 1
    ELSE 0
  END,

  hotel_code;
"""