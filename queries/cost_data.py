DATE_PREDICATE = "stay_date BETWEEN %(start_date)s AND %(end_date)s"


COST_DATA_QUERIES = {
    "arrivalsDepartures": f"""
        SELECT
            hotel.hotel_name,
            stay_date,
            sum(total_arrivals)::bigint AS total_arrivals,
            sum(total_departures)::bigint AS total_departures,
            max(fact.last_updated_at) AS last_updated_at
        FROM functions.arr_dep_data fact
        JOIN functions.hotels hotel USING (enterprise_id)
        WHERE {DATE_PREDICATE}
        GROUP BY hotel.hotel_name, stay_date
        ORDER BY stay_date, hotel.hotel_name
    """,
    "breakfast": f"""
        SELECT
            hotel.hotel_name,
            stay_date,
            sum(breakfast_total)::bigint AS breakfast_total,
            sum(breakfast_net_cost) AS breakfast_net_cost,
            max(fact.last_updated_at) AS last_updated_at
        FROM functions.breakfast_data fact
        JOIN functions.hotels hotel USING (enterprise_id)
        WHERE {DATE_PREDICATE}
        GROUP BY hotel.hotel_name, stay_date
        ORDER BY stay_date, hotel.hotel_name
    """,
    "parking": f"""
        SELECT
            hotel.hotel_name,
            stay_date,
            coalesce(nullif(trim(service), ''), 'Unspecified') AS service,
            sum(total_reservations_using_parking)::bigint
                AS total_reservations_using_parking,
            sum(total_parking_spots)::bigint AS total_parking_spots,
            sum(total_parking_amount_net_value) AS total_parking_amount_net_value,
            max(fact.last_updated_at) AS last_updated_at
        FROM functions.parking_data fact
        JOIN functions.hotels hotel USING (enterprise_id)
        WHERE {DATE_PREDICATE}
        GROUP BY hotel.hotel_name, stay_date, coalesce(nullif(trim(service), ''), 'Unspecified')
        ORDER BY stay_date, hotel.hotel_name, service
    """,
    "roomRevenue": f"""
        SELECT
            hotel.hotel_name,
            stay_date,
            coalesce(nullif(trim(amount_currency), ''), 'Unspecified') AS amount_currency,
            sum(room_revenue_excl_products_1_net) AS room_revenue_excl_products_1_net,
            sum(product_revenue_1_net) AS product_revenue_1_net,
            sum(room_revenue_incl_products_1_net) AS room_revenue_incl_products_1_net,
            max(fact.last_updated_at) AS last_updated_at
        FROM functions.room_revenue_night_data fact
        JOIN functions.hotels hotel USING (enterprise_id)
        WHERE {DATE_PREDICATE}
        GROUP BY hotel.hotel_name, stay_date,
            coalesce(nullif(trim(amount_currency), ''), 'Unspecified')
        ORDER BY stay_date, hotel.hotel_name, amount_currency
    """,
    "payments": f"""
        SELECT
            hotel.hotel_name,
            stay_date,
            coalesce(nullif(trim(amount_currency), ''), 'Unspecified') AS amount_currency,
            sum(total_payment_amount_gross_value) AS total_payment_amount_gross_value,
            max(fact.last_updated_at) AS last_updated_at
        FROM functions.total_payment_data fact
        JOIN functions.hotels hotel USING (enterprise_id)
        WHERE {DATE_PREDICATE}
        GROUP BY hotel.hotel_name, stay_date,
            coalesce(nullif(trim(amount_currency), ''), 'Unspecified')
        ORDER BY stay_date, hotel.hotel_name, amount_currency
    """,
}
