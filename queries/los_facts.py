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
ly_fact_components AS (
    SELECT
        CASE WHEN %(ly_comparison_basis)s = 'sameWeekday'
            THEN arrival_date + 364
            ELSE (arrival_date + INTERVAL '1 year')::date
        END AS arrival_date,
        hotel_code,
        hotel_name,
        los,
        count(*) FILTER (WHERE cancelled_date IS NULL)::bigint
            AS ly_booking_count,
        count(*) FILTER (
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
        )::bigint AS spit_booking_count
    FROM ly_reservation_los
    GROUP BY
        CASE WHEN %(ly_comparison_basis)s = 'sameWeekday'
            THEN arrival_date + 364
            ELSE (arrival_date + INTERVAL '1 year')::date
        END,
        hotel_code,
        hotel_name,
        los
),
ly_facts AS (
    SELECT
        component.arrival_date,
        component.hotel_code,
        component.hotel_name,
        scenario_data.scenario,
        component.los,
        scenario_data.booking_count,
        (component.los::bigint * scenario_data.booking_count)::bigint
            AS night_count
    FROM ly_fact_components component
    CROSS JOIN LATERAL (
        VALUES
            ('ly'::text, component.ly_booking_count),
            ('spit'::text, component.spit_booking_count)
    ) AS scenario_data(scenario, booking_count)
    WHERE scenario_data.booking_count > 0
)
SELECT * FROM current_facts
UNION ALL
SELECT * FROM ly_facts;
"""
