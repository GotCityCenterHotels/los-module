"""Same-point-in-time Cost Data reads from reservation/item lifecycle.

The imported cost tables are deliberately the current/final state.  They cannot
answer SPIT by themselves because a reservation cancelled after the cutoff has
already disappeared from them.  These read-only queries rebuild the comparison
population in Database B with the same boundary used by LOS:

    created_date <= cutoff
    and (cancelled_date is null or cancelled_date > cutoff)

Both the reservation and its item must have existed at the cutoff.  This is
important for shortened stays: the reservation can survive while one of its
room-night items is cancelled later.
"""


LIFECYCLE_PARAMETERS = """
    item.created_utc::date <= %(cutoff_date)s
    AND (item.canceled_utc IS NULL OR item.canceled_utc::date > %(cutoff_date)s)
"""

RESERVATION_LIFECYCLE_PARAMETERS = """
    reservation.created_utc::date <= %(cutoff_date)s
    AND (
        reservation.cancelled_utc IS NULL
        OR reservation.cancelled_utc::date > %(cutoff_date)s
    )
"""

ITEM_DATE_RANGE = """
    item.start_utc >= (
        %(start_date)s::date::timestamp AT TIME ZONE 'Europe/Stockholm'
    )
    AND item.start_utc < (
        (%(end_date)s::date + 1)::timestamp AT TIME ZONE 'Europe/Stockholm'
    )
"""


ROOM_REVENUE_SQL = f"""
    WITH product_categories AS (
        SELECT DISTINCT service_id, accounting_category_id
        FROM product_current
        WHERE tenant_key = 'GCCH'
    ), eligible AS (
        SELECT
            trim(enterprise.name)::text AS hotel_name,
            (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS stay_date,
            coalesce(nullif(trim(item.amount_currency), ''), 'Unspecified')
                AS amount_currency,
            item.type,
            (product.service_id IS NOT NULL) AS is_product,
            item.amount_net_value::numeric AS amount_net_value
        FROM order_item_current item
        JOIN reservation_current reservation
          ON reservation.tenant_key = item.tenant_key
         AND reservation.id = item.service_order_id
        JOIN service_current service
          ON service.tenant_key = reservation.tenant_key
         AND service.id = reservation.service_id
         AND service.name = 'Stay'
        JOIN enterprise_current enterprise
          ON enterprise.tenant_key = service.tenant_key
         AND enterprise.id = service.enterprise_id
        LEFT JOIN product_categories product
          ON product.service_id = item.service_id
         AND product.accounting_category_id = item.accounting_category_id
        WHERE item.tenant_key = 'GCCH'
          AND ({ITEM_DATE_RANGE})
          AND ({LIFECYCLE_PARAMETERS})
          AND ({RESERVATION_LIFECYCLE_PARAMETERS})
          AND (item.type = 'SpaceOrder' OR product.service_id IS NOT NULL)
    )
    SELECT
        hotel_name,
        stay_date::text AS stay_date,
        amount_currency,
        coalesce(sum(amount_net_value) FILTER (
            WHERE type = 'SpaceOrder'
        ), 0)::text AS room_revenue_excl_products_1_net,
        coalesce(sum(amount_net_value) FILTER (
            WHERE type <> 'SpaceOrder' AND is_product
        ), 0)::text AS product_revenue_1_net,
        coalesce(sum(amount_net_value), 0)::text
            AS room_revenue_incl_products_1_net,
        NULL::timestamptz AS last_updated_at
    FROM eligible
    GROUP BY hotel_name, stay_date, amount_currency
    ORDER BY stay_date, hotel_name, amount_currency
"""


PAYMENTS_SQL = f"""
    SELECT
        trim(enterprise.name)::text AS hotel_name,
        (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date::text AS stay_date,
        coalesce(nullif(trim(item.amount_currency), ''), 'Unspecified')
            AS amount_currency,
        coalesce(sum(item.amount_gross_value), 0)::text
            AS total_payment_amount_gross_value,
        NULL::timestamptz AS last_updated_at
    FROM order_item_current item
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = item.tenant_key
     AND enterprise.id = item.enterprise_id
    WHERE item.tenant_key = 'GCCH'
      AND ({ITEM_DATE_RANGE})
      AND ({LIFECYCLE_PARAMETERS})
    GROUP BY enterprise.name,
             (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date,
             coalesce(nullif(trim(item.amount_currency), ''), 'Unspecified')
    ORDER BY stay_date, hotel_name, amount_currency
"""


BREAKFAST_SQL = f"""
    WITH breakfast AS (
        SELECT DISTINCT service_id, accounting_category_id
        FROM product_current
        WHERE tenant_key = 'GCCH'
          AND name = 'Breakfast'
          AND is_active = true
    )
    SELECT
        trim(enterprise.name)::text AS hotel_name,
        (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date::text AS stay_date,
        count(item.amount_net_value)::bigint AS breakfast_total,
        coalesce(sum(item.amount_net_value), 0)::text AS breakfast_net_cost,
        NULL::timestamptz AS last_updated_at
    FROM order_item_current item
    JOIN breakfast
      ON breakfast.service_id = item.service_id
     AND breakfast.accounting_category_id = item.accounting_category_id
    JOIN service_current service
      ON service.tenant_key = item.tenant_key
     AND service.id = item.service_id
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = service.tenant_key
     AND enterprise.id = service.enterprise_id
    WHERE item.tenant_key = 'GCCH'
      AND ({ITEM_DATE_RANGE})
      AND ({LIFECYCLE_PARAMETERS})
    GROUP BY enterprise.name,
             (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date
    ORDER BY stay_date, hotel_name
"""


PARKING_SQL = f"""
    WITH parking_capacity AS (
        SELECT
            resource.tenant_key,
            category.service_id,
            count(DISTINCT resource.id)::bigint AS total_parking_spots
        FROM resource_current resource
        JOIN resource_category_assignment_current assignment
          ON assignment.tenant_key = resource.tenant_key
         AND assignment.resource_id = resource.id
         AND assignment.is_active
        JOIN resource_category_current category
          ON category.tenant_key = assignment.tenant_key
         AND category.id = assignment.category_id
        JOIN service_current service
          ON service.tenant_key = category.tenant_key
         AND service.id = category.service_id
         AND service.name = 'Parkering'
        WHERE resource.is_active
        GROUP BY resource.tenant_key, category.service_id
    )
    SELECT
        trim(enterprise.name)::text AS hotel_name,
        (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date::text AS stay_date,
        service.name::text AS service,
        count(DISTINCT item.service_order_id)::bigint
            AS total_reservations_using_parking,
        coalesce(capacity.total_parking_spots, 0)::bigint AS total_parking_spots,
        coalesce(sum(item.amount_net_value), 0)::text
            AS total_parking_amount_net_value,
        NULL::timestamptz AS last_updated_at
    FROM order_item_current item
    JOIN service_current service
      ON service.tenant_key = item.tenant_key
     AND service.id = item.service_id
     AND service.name = 'Parkering'
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = service.tenant_key
     AND enterprise.id = service.enterprise_id
    LEFT JOIN parking_capacity capacity
      ON capacity.tenant_key = service.tenant_key
     AND capacity.service_id = service.id
    WHERE item.tenant_key = 'GCCH'
      AND ({ITEM_DATE_RANGE})
      AND ({LIFECYCLE_PARAMETERS})
    GROUP BY enterprise.name, service.name,
             (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date,
             capacity.total_parking_spots
    ORDER BY stay_date, hotel_name, service
"""


ARRIVALS_DEPARTURES_SQL = f"""
    WITH relevant_reservations AS (
        SELECT DISTINCT reservation.id AS reservation_id
        FROM order_item_current item
        JOIN reservation_current reservation
          ON reservation.tenant_key = item.tenant_key
         AND reservation.id = item.service_order_id
        JOIN service_current service
          ON service.tenant_key = reservation.tenant_key
         AND service.id = reservation.service_id
         AND service.name = 'Stay'
        WHERE item.tenant_key = 'GCCH'
          AND item.type = 'SpaceOrder'
          -- The night before start_date is needed for a departure on the first
          -- requested day.  Once the small reservation-id set is known, the next
          -- CTE reads every eligible night of those stays so neither endpoint is
          -- invented by truncating a stay at the requested range.
          AND item.start_utc >= (
              (%(start_date)s::date - 1)::timestamp
                  AT TIME ZONE 'Europe/Stockholm'
          )
          AND item.start_utc < (
              (%(end_date)s::date + 1)::timestamp
                  AT TIME ZONE 'Europe/Stockholm'
          )
          AND ({LIFECYCLE_PARAMETERS})
          AND ({RESERVATION_LIFECYCLE_PARAMETERS})
    ), eligible_nights AS (
        SELECT DISTINCT
            reservation.id AS reservation_id,
            trim(enterprise.name)::text AS hotel_name,
            (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS stay_date
        FROM order_item_current item
        JOIN reservation_current reservation
          ON reservation.tenant_key = item.tenant_key
         AND reservation.id = item.service_order_id
        JOIN relevant_reservations relevant
          ON relevant.reservation_id = reservation.id
        JOIN service_current service
          ON service.tenant_key = reservation.tenant_key
         AND service.id = reservation.service_id
         AND service.name = 'Stay'
        JOIN enterprise_current enterprise
          ON enterprise.tenant_key = service.tenant_key
         AND enterprise.id = service.enterprise_id
        WHERE item.tenant_key = 'GCCH'
          AND item.type = 'SpaceOrder'
          AND ({LIFECYCLE_PARAMETERS})
          AND ({RESERVATION_LIFECYCLE_PARAMETERS})
    ), stays AS (
        SELECT reservation_id, hotel_name,
               min(stay_date) AS arrival_date,
               max(stay_date) + 1 AS departure_date
        FROM eligible_nights
        GROUP BY reservation_id, hotel_name
    ), events AS (
        SELECT hotel_name, arrival_date AS stay_date,
               count(DISTINCT reservation_id)::bigint AS total_arrivals,
               0::bigint AS total_departures
        FROM stays
        GROUP BY hotel_name, arrival_date
        UNION ALL
        SELECT hotel_name, departure_date AS stay_date,
               0::bigint AS total_arrivals,
               count(DISTINCT reservation_id)::bigint AS total_departures
        FROM stays
        GROUP BY hotel_name, departure_date
    )
    SELECT hotel_name, stay_date::text AS stay_date,
           sum(total_arrivals)::bigint AS total_arrivals,
           sum(total_departures)::bigint AS total_departures,
           NULL::timestamptz AS last_updated_at
    FROM events
    WHERE stay_date BETWEEN %(start_date)s AND %(end_date)s
    GROUP BY hotel_name, stay_date
    ORDER BY stay_date, hotel_name
"""


CLEANING_ALLOCATIONS_SQL = f"""
    WITH assigned_category AS (
        SELECT assignment.tenant_key, assignment.resource_id,
               category.id AS category_id, category.space_name AS category_name
        FROM resource_category_assignment_current assignment
        JOIN resource_category_current category
          ON category.tenant_key = assignment.tenant_key
         AND category.id = assignment.category_id
        WHERE assignment.is_active AND category.type = 'Room'
    ), relevant_reservations AS (
        SELECT DISTINCT reservation.id AS reservation_id
        FROM order_item_current item
        JOIN reservation_current reservation
          ON reservation.tenant_key = item.tenant_key
         AND reservation.id = item.service_order_id
        JOIN service_current service
          ON service.tenant_key = reservation.tenant_key
         AND service.id = reservation.service_id
         AND service.name = 'Stay'
        WHERE item.tenant_key = 'GCCH'
          AND item.type = 'SpaceOrder'
          AND ({ITEM_DATE_RANGE})
          AND ({LIFECYCLE_PARAMETERS})
          AND ({RESERVATION_LIFECYCLE_PARAMETERS})
    ), eligible_nights AS (
        SELECT DISTINCT
            reservation.id AS reservation_id,
            trim(enterprise.name)::text AS hotel_name,
            (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS stay_date,
            coalesce(assigned.category_id, requested.id)::text AS category_id,
            trim(coalesce(assigned.category_name, requested.space_name))::text
                AS category_name,
            greatest(coalesce(persons.occupancy, 0), 1)::int AS occupancy
        FROM order_item_current item
        JOIN reservation_current reservation
          ON reservation.tenant_key = item.tenant_key
         AND reservation.id = item.service_order_id
        JOIN relevant_reservations relevant
          ON relevant.reservation_id = reservation.id
        JOIN service_current service
          ON service.tenant_key = reservation.tenant_key
         AND service.id = reservation.service_id
         AND service.name = 'Stay'
        JOIN enterprise_current enterprise
          ON enterprise.tenant_key = service.tenant_key
         AND enterprise.id = service.enterprise_id
        LEFT JOIN resource_category_current requested
          ON requested.tenant_key = reservation.tenant_key
         AND requested.id = reservation.requested_resource_category_id
         AND requested.type = 'Room'
        LEFT JOIN assigned_category assigned
          ON assigned.tenant_key = reservation.tenant_key
         AND assigned.resource_id = reservation.assigned_resource_id
        LEFT JOIN LATERAL (
            SELECT sum(coalesce(
                nullif(entry ->> 'Count', ''),
                nullif(entry ->> 'count', ''),
                '0'
            )::int)::int AS occupancy
            FROM jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(reservation.person_counts::jsonb) = 'array'
                    THEN reservation.person_counts::jsonb
                    ELSE '[]'::jsonb
                END
            ) entry
        ) persons ON true
        WHERE item.tenant_key = 'GCCH'
          AND item.type = 'SpaceOrder'
          AND ({LIFECYCLE_PARAMETERS})
          AND ({RESERVATION_LIFECYCLE_PARAMETERS})
          AND coalesce(assigned.category_id, requested.id) IS NOT NULL
    ), stay_lengths AS (
        SELECT reservation_id, hotel_name, count(*)::numeric AS stay_nights
        FROM eligible_nights
        GROUP BY reservation_id, hotel_name
    )
    SELECT
        night.hotel_name,
        night.stay_date::text AS stay_date,
        night.category_name,
        night.occupancy,
        sum(1::numeric / length.stay_nights)::text AS allocated_cleanings,
        NULL::timestamptz AS last_updated_at
    FROM eligible_nights night
    JOIN stay_lengths length USING (reservation_id, hotel_name)
    WHERE night.stay_date BETWEEN %(start_date)s AND %(end_date)s
    GROUP BY night.hotel_name, night.stay_date, night.category_id,
             night.category_name, night.occupancy
    ORDER BY night.stay_date, night.hotel_name, night.category_name, night.occupancy
"""


# The distribution rulebook lives in Database A.  The lifecycle query therefore
# returns its exact as-of mix and the browser prices that mix with the same saved
# rulebook used for every other GOP line.
DISTRIBUTION_MIX_SQL = f"""
    SELECT
        trim(enterprise.name)::text AS hotel_name,
        (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date::text AS stay_date,
        nullif(trim(reservation.origin), '')::text AS origin,
        nullif(trim(agency.name), '')::text AS travel_agency,
        nullif(trim(rate.rate_name), '')::text AS rate_name,
        coalesce(sum(item.amount_net_value), 0)::text AS room_revenue_net,
        NULL::timestamptz AS last_updated_at
    FROM order_item_current item
    JOIN reservation_current reservation
      ON reservation.tenant_key = item.tenant_key
     AND reservation.id = item.service_order_id
    JOIN service_current service
      ON service.tenant_key = reservation.tenant_key
     AND service.id = reservation.service_id
     AND service.name = 'Stay'
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = service.tenant_key
     AND enterprise.id = service.enterprise_id
    LEFT JOIN staging.travel_agency agency
      ON agency.id::text = reservation.travel_agency_id::text
    LEFT JOIN rate_current rate
      ON rate.id::text = reservation.rate_id::text
    WHERE item.tenant_key = 'GCCH'
      AND item.type = 'SpaceOrder'
      AND ({ITEM_DATE_RANGE})
      AND ({LIFECYCLE_PARAMETERS})
      AND ({RESERVATION_LIFECYCLE_PARAMETERS})
    GROUP BY enterprise.name,
             (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date,
             reservation.origin, agency.name, rate.rate_name
    ORDER BY stay_date, hotel_name, origin, travel_agency, rate_name
"""


COST_SPIT_QUERIES = {
    "arrivalsDepartures": ARRIVALS_DEPARTURES_SQL,
    "breakfast": BREAKFAST_SQL,
    "parking": PARKING_SQL,
    "roomRevenue": ROOM_REVENUE_SQL,
    "payments": PAYMENTS_SQL,
    "cleaningAllocations": CLEANING_ALLOCATIONS_SQL,
    "distributionMix": DISTRIBUTION_MIX_SQL,
}
