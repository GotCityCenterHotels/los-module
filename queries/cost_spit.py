"""Exact, bounded Same Point In Time facts for Cost Data.

The imported Cost Data tables contain the final state, so SPIT has to read the
reservation/item lifecycle in Database B. The first implementation issued seven
independent queries and scanned ``order_item_current`` nine times. This query
materializes the requested item window once, expands the relevant room stays
once, and derives every response dataset from those two bounded sets.

Both lifecycle levels use the LOS boundary:

    created_date <= cutoff
    and (cancelled_date is null or cancelled_date > cutoff)
"""


COST_SPIT_DATASETS = (
    "arrivalsDepartures",
    "breakfast",
    "parking",
    "roomRevenue",
    "payments",
    "cleaningAllocations",
    "distributionMix",
)


ITEM_LIFECYCLE = """
    item.created_utc::date <= %(cutoff_date)s
    AND (item.canceled_utc IS NULL OR item.canceled_utc::date > %(cutoff_date)s)
"""

RESERVATION_LIFECYCLE = """
    reservation_created_utc::date <= %(cutoff_date)s
    AND (
        reservation_cancelled_utc IS NULL
        OR reservation_cancelled_utc::date > %(cutoff_date)s
    )
"""


COST_SPIT_SQL = f"""
WITH
product_categories AS (
    SELECT DISTINCT service_id, accounting_category_id
    FROM product_current
    WHERE tenant_key = 'GCCH'
),
breakfast_categories AS (
    SELECT DISTINCT service_id, accounting_category_id
    FROM product_current
    WHERE tenant_key = 'GCCH'
      AND name = 'Breakfast'
      AND is_active = true
),
parking_capacity AS (
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
),
assigned_category AS (
    SELECT DISTINCT ON (assignment.tenant_key, assignment.resource_id)
           assignment.tenant_key, assignment.resource_id,
           category.id AS category_id, category.space_name AS category_name
    FROM resource_category_assignment_current assignment
    JOIN resource_category_current category
      ON category.tenant_key = assignment.tenant_key
     AND category.id = assignment.category_id
    WHERE assignment.is_active AND category.type = 'Room'
    ORDER BY assignment.tenant_key, assignment.resource_id, category.id
),
-- The only range scan of order_item_current. One extra preceding night is
-- included solely so a stay ending on start_date is available to departures.
scoped_items AS MATERIALIZED (
    SELECT
        item.tenant_key,
        item.service_order_id,
        item.service_id,
        item.type,
        (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS stay_date,
        coalesce(nullif(trim(item.amount_currency), ''), 'Unspecified')
            AS amount_currency,
        item.amount_net_value::numeric AS amount_net_value,
        item.amount_gross_value::numeric AS amount_gross_value,
        item.enterprise_id AS item_enterprise_id,
        item_service.enterprise_id AS item_service_enterprise_id,
        (item_service.name = 'Parkering') AS is_parking,
        (product.service_id IS NOT NULL) AS is_product,
        (breakfast.service_id IS NOT NULL) AS is_breakfast,
        reservation.id AS reservation_id,
        reservation.created_utc AS reservation_created_utc,
        reservation.cancelled_utc AS reservation_cancelled_utc,
        stay_service.enterprise_id AS stay_enterprise_id,
        (stay_service.name = 'Stay') AS is_stay
    FROM order_item_current item
    LEFT JOIN service_current item_service
      ON item_service.tenant_key = item.tenant_key
     AND item_service.id = item.service_id
    LEFT JOIN reservation_current reservation
      ON reservation.tenant_key = item.tenant_key
     AND reservation.id = item.service_order_id
    LEFT JOIN service_current stay_service
      ON stay_service.tenant_key = reservation.tenant_key
     AND stay_service.id = reservation.service_id
    LEFT JOIN product_categories product
      ON product.service_id = item.service_id
     AND product.accounting_category_id = item.accounting_category_id
    LEFT JOIN breakfast_categories breakfast
      ON breakfast.service_id = item.service_id
     AND breakfast.accounting_category_id = item.accounting_category_id
    WHERE item.tenant_key = 'GCCH'
      AND item.start_utc >= (
          (%(start_date)s::date - 1)::timestamp
              AT TIME ZONE 'Europe/Stockholm'
      )
      AND item.start_utc < (
          (%(end_date)s::date + 1)::timestamp AT TIME ZONE 'Europe/Stockholm'
      )
      AND ({ITEM_LIFECYCLE})
),
-- Resolve reservation-level dimensions once rather than once per room night.
relevant_reservations AS MATERIALIZED (
    SELECT DISTINCT ON (tenant_key, reservation_id)
        tenant_key,
        reservation_id,
        stay_enterprise_id AS enterprise_id
    FROM scoped_items
    WHERE type = 'SpaceOrder'
      AND is_stay
      AND reservation_id IS NOT NULL
      AND stay_enterprise_id IS NOT NULL
      AND ({RESERVATION_LIFECYCLE})
    ORDER BY tenant_key, reservation_id
),
reservation_dimensions AS MATERIALIZED (
    SELECT
        reservation.tenant_key,
        reservation.reservation_id,
        reservation.enterprise_id,
        coalesce(assigned.category_id, requested.id)::text AS category_id,
        trim(coalesce(assigned.category_name, requested.space_name))::text
            AS category_name,
        greatest(coalesce(persons.occupancy, 0), 1)::int AS occupancy
    FROM relevant_reservations reservation
    JOIN reservation_current source_reservation
      ON source_reservation.tenant_key = reservation.tenant_key
     AND source_reservation.id = reservation.reservation_id
    LEFT JOIN resource_category_current requested
      ON requested.tenant_key = reservation.tenant_key
     AND requested.id = source_reservation.requested_resource_category_id
     AND requested.type = 'Room'
    LEFT JOIN assigned_category assigned
      ON assigned.tenant_key = reservation.tenant_key
     AND assigned.resource_id = source_reservation.assigned_resource_id
    LEFT JOIN LATERAL (
        SELECT sum(coalesce(
            nullif(entry ->> 'Count', ''),
            nullif(entry ->> 'count', ''),
            '0'
        )::int)::int AS occupancy
        FROM jsonb_array_elements(
            CASE
                WHEN jsonb_typeof(source_reservation.person_counts::jsonb) = 'array'
                THEN source_reservation.person_counts::jsonb
                ELSE '[]'::jsonb
            END
        ) entry
    ) persons ON true
),
-- The only second access to order_item_current is by relevant reservation id.
-- Whole stays are required so range edges do not invent stay endpoints or
-- over-allocate cleaning shares.
eligible_nights AS MATERIALIZED (
    SELECT DISTINCT
        reservation.reservation_id,
        reservation.enterprise_id,
        (item.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS stay_date,
        reservation.category_id,
        reservation.category_name,
        reservation.occupancy
    FROM reservation_dimensions reservation
    JOIN order_item_current item
      ON item.tenant_key = reservation.tenant_key
     AND item.service_order_id = reservation.reservation_id
     AND item.type = 'SpaceOrder'
    WHERE item.start_utc IS NOT NULL
      AND ({ITEM_LIFECYCLE})
),
stays AS (
    SELECT reservation_id, enterprise_id,
           min(stay_date) AS arrival_date,
           max(stay_date) + 1 AS departure_date
    FROM eligible_nights
    GROUP BY reservation_id, enterprise_id
),
arrival_events AS (
    SELECT enterprise_id, arrival_date AS stay_date,
           count(*)::bigint AS total_arrivals, 0::bigint AS total_departures
    FROM stays
    GROUP BY enterprise_id, arrival_date
    UNION ALL
    SELECT enterprise_id, departure_date AS stay_date,
           0::bigint AS total_arrivals, count(*)::bigint AS total_departures
    FROM stays
    GROUP BY enterprise_id, departure_date
),
arrivals_departures AS (
    SELECT trim(enterprise.name)::text AS hotel_name,
           event.stay_date::text AS stay_date,
           sum(event.total_arrivals)::bigint AS total_arrivals,
           sum(event.total_departures)::bigint AS total_departures,
           NULL::timestamptz AS last_updated_at
    FROM arrival_events event
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = 'GCCH'
     AND enterprise.id = event.enterprise_id
    WHERE event.stay_date BETWEEN %(start_date)s AND %(end_date)s
    GROUP BY enterprise.name, event.stay_date
),
breakfast AS (
    SELECT trim(enterprise.name)::text AS hotel_name,
           item.stay_date::text AS stay_date,
           count(item.amount_net_value)::bigint AS breakfast_total,
           coalesce(sum(item.amount_net_value), 0)::text AS breakfast_net_cost,
           NULL::timestamptz AS last_updated_at
    FROM scoped_items item
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = item.tenant_key
     AND enterprise.id = item.item_service_enterprise_id
    WHERE item.stay_date BETWEEN %(start_date)s AND %(end_date)s
      AND item.is_breakfast
    GROUP BY enterprise.name, item.stay_date
),
parking AS (
    SELECT trim(enterprise.name)::text AS hotel_name,
           item.stay_date::text AS stay_date,
           'Parkering'::text AS service,
           count(DISTINCT item.service_order_id)::bigint
               AS total_reservations_using_parking,
           coalesce(capacity.total_parking_spots, 0)::bigint
               AS total_parking_spots,
           coalesce(sum(item.amount_net_value), 0)::text
               AS total_parking_amount_net_value,
           NULL::timestamptz AS last_updated_at
    FROM scoped_items item
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = item.tenant_key
     AND enterprise.id = item.item_service_enterprise_id
    LEFT JOIN parking_capacity capacity
      ON capacity.tenant_key = item.tenant_key
     AND capacity.service_id = item.service_id
    WHERE item.stay_date BETWEEN %(start_date)s AND %(end_date)s
      AND item.is_parking
    GROUP BY enterprise.name, item.stay_date, capacity.total_parking_spots
),
room_revenue AS (
    SELECT trim(enterprise.name)::text AS hotel_name,
           item.stay_date::text AS stay_date,
           item.amount_currency,
           coalesce(sum(item.amount_net_value) FILTER (
               WHERE item.type = 'SpaceOrder'
           ), 0)::text AS room_revenue_excl_products_1_net,
           coalesce(sum(item.amount_net_value) FILTER (
               WHERE item.type <> 'SpaceOrder' AND item.is_product
           ), 0)::text AS product_revenue_1_net,
           coalesce(sum(item.amount_net_value), 0)::text
               AS room_revenue_incl_products_1_net,
           NULL::timestamptz AS last_updated_at
    FROM scoped_items item
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = item.tenant_key
     AND enterprise.id = item.stay_enterprise_id
    WHERE item.stay_date BETWEEN %(start_date)s AND %(end_date)s
      AND item.is_stay
      AND (item.type = 'SpaceOrder' OR item.is_product)
      AND ({RESERVATION_LIFECYCLE})
    GROUP BY enterprise.name, item.stay_date, item.amount_currency
),
payments AS (
    SELECT trim(enterprise.name)::text AS hotel_name,
           item.stay_date::text AS stay_date,
           item.amount_currency,
           coalesce(sum(item.amount_gross_value), 0)::text
               AS total_payment_amount_gross_value,
           NULL::timestamptz AS last_updated_at
    FROM scoped_items item
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = item.tenant_key
     AND enterprise.id = item.item_enterprise_id
    WHERE item.stay_date BETWEEN %(start_date)s AND %(end_date)s
    GROUP BY enterprise.name, item.stay_date, item.amount_currency
),
stay_lengths AS (
    SELECT reservation_id, enterprise_id, count(*)::numeric AS stay_nights
    FROM eligible_nights
    GROUP BY reservation_id, enterprise_id
),
cleaning_allocations AS (
    SELECT trim(enterprise.name)::text AS hotel_name,
           night.stay_date::text AS stay_date,
           night.category_name, night.occupancy,
           sum(1::numeric / length.stay_nights)::text AS allocated_cleanings,
           NULL::timestamptz AS last_updated_at
    FROM eligible_nights night
    JOIN stay_lengths length USING (reservation_id, enterprise_id)
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = 'GCCH'
     AND enterprise.id = night.enterprise_id
    WHERE night.stay_date BETWEEN %(start_date)s AND %(end_date)s
      AND night.category_id IS NOT NULL
    GROUP BY enterprise.name, night.stay_date, night.category_id,
             night.category_name, night.occupancy
),
distribution_mix AS (
    SELECT trim(enterprise.name)::text AS hotel_name,
           item.stay_date::text AS stay_date,
           nullif(trim(reservation.origin), '')::text AS origin,
           nullif(trim(agency.name), '')::text AS travel_agency,
           nullif(trim(rate.rate_name), '')::text AS rate_name,
           coalesce(sum(item.amount_net_value), 0)::text AS room_revenue_net,
           NULL::timestamptz AS last_updated_at
    FROM scoped_items item
    JOIN reservation_current reservation
      ON reservation.tenant_key = item.tenant_key
     AND reservation.id = item.reservation_id
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = item.tenant_key
     AND enterprise.id = item.stay_enterprise_id
    LEFT JOIN staging.travel_agency agency
      ON agency.id::text = reservation.travel_agency_id::text
    LEFT JOIN rate_current rate
      ON rate.id::text = reservation.rate_id::text
    WHERE item.stay_date BETWEEN %(start_date)s AND %(end_date)s
      AND item.type = 'SpaceOrder'
      AND item.is_stay
      AND ({RESERVATION_LIFECYCLE})
    GROUP BY enterprise.name, item.stay_date, reservation.origin,
             agency.name, rate.rate_name
),
result_rows AS (
    SELECT 1 AS dataset_order, 'arrivalsDepartures'::text AS dataset,
           to_jsonb(fact) AS payload
    FROM arrivals_departures fact
    UNION ALL
    SELECT 2, 'breakfast', to_jsonb(fact) FROM breakfast fact
    UNION ALL
    SELECT 3, 'parking', to_jsonb(fact) FROM parking fact
    UNION ALL
    SELECT 4, 'roomRevenue', to_jsonb(fact) FROM room_revenue fact
    UNION ALL
    SELECT 5, 'payments', to_jsonb(fact) FROM payments fact
    UNION ALL
    SELECT 6, 'cleaningAllocations', to_jsonb(fact)
    FROM cleaning_allocations fact
    UNION ALL
    SELECT 7, 'distributionMix', to_jsonb(fact) FROM distribution_mix fact
)
SELECT dataset, payload
FROM result_rows
ORDER BY dataset_order, payload ->> 'stay_date', payload ->> 'hotel_name'
"""
