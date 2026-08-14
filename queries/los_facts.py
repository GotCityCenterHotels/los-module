LOS_FACTS_SQL = """
WITH
current_reservations AS MATERIALIZED (
    SELECT
        rc.id,
        rc.number::text AS reservation_id,
        ec.id::text AS hotel_code,
        trim(ec.name)::text AS hotel_name,
        (rc.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS arrival_date
    FROM reservation_current rc
    JOIN service_current sc
      ON sc.id = rc.service_id
     AND sc.name = 'Stay'
    JOIN enterprise_current ec
      ON ec.id = sc.enterprise_id
     AND ec.tenant_key = 'GCCH'
    WHERE rc.number IS NOT NULL
      AND rc.start_utc IS NOT NULL
      AND ec.name IS NOT NULL
      AND trim(ec.name) <> ''
      AND rc.start_utc >= (
          %(start_date)s::date::timestamp AT TIME ZONE 'Europe/Stockholm'
      )
      AND rc.start_utc < (
          (%(end_date)s::date + 1)::timestamp AT TIME ZONE 'Europe/Stockholm'
      )
),
current_reservation_los AS (
    SELECT
        reservation.reservation_id,
        reservation.hotel_code,
        reservation.hotel_name,
        reservation.arrival_date,
        count(DISTINCT (
            item.start_utc AT TIME ZONE 'Europe/Stockholm'
        )::date)::int AS los
    FROM current_reservations reservation
    JOIN order_item_current item
      ON item.service_order_id = reservation.id
     AND item.type = 'SpaceOrder'
     AND item.start_utc IS NOT NULL
     AND item.canceled_utc IS NULL
    GROUP BY
        reservation.reservation_id,
        reservation.hotel_code,
        reservation.hotel_name,
        reservation.arrival_date
),
current_facts AS (
    SELECT
        arrival_date,
        hotel_code,
        hotel_name,
        'current'::text AS scenario,
        los,
        count(*)::bigint AS booking_count,
        (los::bigint * count(*))::bigint AS night_count
    FROM current_reservation_los
    GROUP BY arrival_date, hotel_code, hotel_name, los
),
ly_reservations AS MATERIALIZED (
    SELECT
        rc.id,
        rc.number::text AS reservation_id,
        ec.id::text AS hotel_code,
        trim(ec.name)::text AS hotel_name,
        (rc.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS arrival_date,
        rc.created_utc::date AS created_date
    FROM reservation_current rc
    JOIN service_current sc
      ON sc.id = rc.service_id
     AND sc.name = 'Stay'
    JOIN enterprise_current ec
      ON ec.id = sc.enterprise_id
     AND ec.tenant_key = 'GCCH'
    WHERE rc.number IS NOT NULL
      AND rc.start_utc IS NOT NULL
      AND ec.name IS NOT NULL
      AND trim(ec.name) <> ''
      AND rc.start_utc >= (
          (
              CASE WHEN %(ly_comparison_basis)s = 'sameWeekday'
                  THEN %(start_date)s::date - 364
                  ELSE (%(start_date)s::date - INTERVAL '1 year')::date
              END
          )::timestamp AT TIME ZONE 'Europe/Stockholm'
      )
      AND rc.start_utc < (
          (
              CASE WHEN %(ly_comparison_basis)s = 'sameWeekday'
                  THEN %(end_date)s::date - 364 + 1
                  ELSE ((%(end_date)s::date - INTERVAL '1 year')::date + 1)
              END
          )::timestamp AT TIME ZONE 'Europe/Stockholm'
      )
),
ly_reservation_los AS (
    SELECT
        reservation.reservation_id,
        reservation.hotel_code,
        reservation.hotel_name,
        reservation.arrival_date,
        reservation.created_date,
        item.canceled_utc::date AS cancelled_date,
        count(DISTINCT (
            item.start_utc AT TIME ZONE 'Europe/Stockholm'
        )::date)::int AS los
    FROM ly_reservations reservation
    JOIN order_item_current item
      ON item.service_order_id = reservation.id
     AND item.type = 'SpaceOrder'
     AND item.start_utc IS NOT NULL
    WHERE item.canceled_utc IS NULL
       OR (
           reservation.created_date <= (
               CASE WHEN %(ly_comparison_basis)s = 'sameWeekday'
                   THEN CURRENT_DATE - 364
                   ELSE (CURRENT_DATE - INTERVAL '1 year')::date
               END
           )
           AND item.canceled_utc::date > (
               CASE WHEN %(ly_comparison_basis)s = 'sameWeekday'
                   THEN CURRENT_DATE - 364
                   ELSE (CURRENT_DATE - INTERVAL '1 year')::date
               END
           )
       )
    GROUP BY
        reservation.reservation_id,
        reservation.hotel_code,
        reservation.hotel_name,
        reservation.arrival_date,
        reservation.created_date,
        item.canceled_utc::date
),
ly_facts AS (
    -- Last year's final state: the single cancelled_date IS NULL row per
    -- reservation already carries its surviving length of stay.
    SELECT
        CASE WHEN %(ly_comparison_basis)s = 'sameWeekday'
            THEN arrival_date + 364
            ELSE (arrival_date + INTERVAL '1 year')::date
        END AS arrival_date,
        hotel_code,
        hotel_name,
        'ly'::text AS scenario,
        los,
        count(*)::bigint AS booking_count,
        (los::bigint * count(*))::bigint AS night_count
    FROM ly_reservation_los
    WHERE cancelled_date IS NULL
    GROUP BY 1, 2, 3, 5
),
spit_reservation AS (
    -- Same point in time: rebuild each reservation as it stood at the cutoff.
    --
    -- ly_reservation_los holds one row per distinct cancellation date, so a
    -- reservation shortened after the cutoff appears as several rows. Counting
    -- those rows treated one booking as several and split its length of stay
    -- across them. Collapse to one row per reservation before counting.
    --
    -- Kept deliberately identical in shape to the read model's spit_reservation
    -- CTE in services/los_sync_service.py, so validate_los_read_model.py
    -- compares two implementations of the same definition.
    SELECT
        CASE WHEN %(ly_comparison_basis)s = 'sameWeekday'
            THEN arrival_date + 364
            ELSE (arrival_date + INTERVAL '1 year')::date
        END AS arrival_date,
        hotel_code,
        hotel_name,
        reservation_id,
        sum(los)::int AS los
    FROM ly_reservation_los
    WHERE created_date <= (
            CASE WHEN %(ly_comparison_basis)s = 'sameWeekday'
                THEN CURRENT_DATE - 364
                ELSE (CURRENT_DATE - INTERVAL '1 year')::date
            END
        )
      AND (
          cancelled_date IS NULL
          OR cancelled_date > (
              CASE WHEN %(ly_comparison_basis)s = 'sameWeekday'
                  THEN CURRENT_DATE - 364
                  ELSE (CURRENT_DATE - INTERVAL '1 year')::date
              END
          )
      )
    GROUP BY 1, 2, 3, 4
),
spit_facts AS (
    SELECT
        arrival_date,
        hotel_code,
        hotel_name,
        'spit'::text AS scenario,
        los,
        count(*)::bigint AS booking_count,
        (los::bigint * count(*))::bigint AS night_count
    FROM spit_reservation
    GROUP BY 1, 2, 3, 5
)
SELECT * FROM current_facts
UNION ALL
SELECT * FROM ly_facts
UNION ALL
SELECT * FROM spit_facts;
"""
