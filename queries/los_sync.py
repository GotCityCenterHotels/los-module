SOURCE_CLOCK_SQL = """
SELECT CURRENT_TIMESTAMP AS upper_watermark, CURRENT_DATE AS as_of_date
"""


AFFECTED_RESERVATIONS_SQL = """
WITH changed_reservations AS (
    SELECT rc.id, rc.number
    FROM reservation_current rc
    WHERE rc.snapshot_valid_from > %(watermark_from)s
      AND rc.snapshot_valid_from <= %(watermark_to)s

    UNION

    SELECT rc.id, rc.number
    FROM order_item_current item
    JOIN reservation_current rc ON rc.id = item.service_order_id
    WHERE item.snapshot_valid_from > %(watermark_from)s
      AND item.snapshot_valid_from <= %(watermark_to)s

    UNION

    SELECT rc.id, rc.number
    FROM service_current service
    JOIN reservation_current rc ON rc.service_id = service.id
    WHERE service.snapshot_valid_from > %(watermark_from)s
      AND service.snapshot_valid_from <= %(watermark_to)s

    UNION

    SELECT rc.id, rc.number
    FROM enterprise_current enterprise
    JOIN service_current service ON service.enterprise_id = enterprise.id
    JOIN reservation_current rc ON rc.service_id = service.id
    WHERE enterprise.snapshot_valid_from > %(watermark_from)s
      AND enterprise.snapshot_valid_from <= %(watermark_to)s
)
SELECT DISTINCT id AS reservation_id, number::text AS reservation_number
FROM changed_reservations
ORDER BY reservation_id
"""


_IDENTITY_SOURCE_TEMPLATE = """
SELECT
    rc.id AS reservation_id,
    rc.number::text AS reservation_number,
    greatest(
        rc.snapshot_valid_from,
        service.snapshot_valid_from,
        enterprise.snapshot_valid_from
    ) AS source_updated_at
FROM reservation_current rc
JOIN service_current service
  ON service.id = rc.service_id
 AND service.name = 'Stay'
JOIN enterprise_current enterprise
  ON enterprise.id = service.enterprise_id
 AND enterprise.tenant_key = 'GCCH'
WHERE rc.number IS NOT NULL
  {reservation_filter}
ORDER BY rc.id
"""


FULL_IDENTITY_SOURCE_SQL = _IDENTITY_SOURCE_TEMPLATE.format(
    reservation_filter=""
)
FILTERED_IDENTITY_SOURCE_SQL = _IDENTITY_SOURCE_TEMPLATE.format(
    reservation_filter="AND rc.number = ANY(%(reservation_numbers)s)"
)


_FACT_SOURCE_TEMPLATE = """
WITH eligible_reservations AS MATERIALIZED (
    SELECT
        rc.id,
        rc.number::text AS reservation_number,
        ec.id::text AS enterprise_id,
        trim(ec.name)::text AS hotel_name,
        (rc.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS arrival_date,
        rc.created_utc::date AS created_date,
        greatest(
            rc.snapshot_valid_from,
            service.snapshot_valid_from,
            ec.snapshot_valid_from
        ) AS reservation_updated_at
    FROM reservation_current rc
    JOIN service_current service
      ON service.id = rc.service_id
     AND service.name = 'Stay'
    JOIN enterprise_current ec
      ON ec.id = service.enterprise_id
     AND ec.tenant_key = 'GCCH'
    WHERE rc.number IS NOT NULL
      AND rc.start_utc IS NOT NULL
      AND ec.name IS NOT NULL
      AND trim(ec.name) <> ''
      {reservation_filter}
),
current_facts AS (
    SELECT
        md5(concat_ws(
            '|', 'current', reservation.reservation_number,
            reservation.enterprise_id, reservation.arrival_date::text
        )) AS fact_key,
        'current'::text AS fact_kind,
        reservation.reservation_number,
        reservation.enterprise_id,
        max(reservation.hotel_name)::text AS hotel_name,
        reservation.arrival_date,
        NULL::date AS created_date,
        NULL::date AS cancelled_date,
        count(DISTINCT (
            item.start_utc AT TIME ZONE 'Europe/Stockholm'
        )::date)::int AS los,
        max(greatest(
            reservation.reservation_updated_at,
            item.snapshot_valid_from
        )) AS source_updated_at
    FROM eligible_reservations reservation
    JOIN order_item_current item
      ON item.service_order_id = reservation.id
     AND item.type = 'SpaceOrder'
     AND item.start_utc IS NOT NULL
     AND item.canceled_utc IS NULL
    GROUP BY
        reservation.reservation_number,
        reservation.enterprise_id,
        reservation.arrival_date
),
historical_facts AS (
    SELECT
        md5(concat_ws(
            '|', 'historical', reservation.reservation_number,
            reservation.enterprise_id, reservation.arrival_date::text,
            coalesce(reservation.created_date::text, '<null>'),
            coalesce(item.canceled_utc::date::text, '<null>')
        )) AS fact_key,
        'historical'::text AS fact_kind,
        reservation.reservation_number,
        reservation.enterprise_id,
        max(reservation.hotel_name)::text AS hotel_name,
        reservation.arrival_date,
        reservation.created_date,
        item.canceled_utc::date AS cancelled_date,
        count(DISTINCT (
            item.start_utc AT TIME ZONE 'Europe/Stockholm'
        )::date)::int AS los,
        max(greatest(
            reservation.reservation_updated_at,
            item.snapshot_valid_from
        )) AS source_updated_at
    FROM eligible_reservations reservation
    JOIN order_item_current item
      ON item.service_order_id = reservation.id
     AND item.type = 'SpaceOrder'
     AND item.start_utc IS NOT NULL
    GROUP BY
        reservation.reservation_number,
        reservation.enterprise_id,
        reservation.arrival_date,
        reservation.created_date,
        item.canceled_utc::date
)
SELECT * FROM current_facts
UNION ALL
SELECT * FROM historical_facts
ORDER BY reservation_number, fact_kind, arrival_date, cancelled_date
"""


FULL_FACT_SOURCE_SQL = _FACT_SOURCE_TEMPLATE.format(reservation_filter="")
FILTERED_FACT_SOURCE_SQL = _FACT_SOURCE_TEMPLATE.format(
    reservation_filter="AND rc.number = ANY(%(reservation_numbers)s)"
)
