INSERT INTO functions.room_revenue_night_data (
    room_revenue_night_data_key,
    tenant_key,
    enterprise_id,
    hotel_name,
    local_timezone,
    stay_date,
    amount_currency,
    room_revenue_excl_products_1_net,
    product_revenue_1_net,
    room_revenue_incl_products_1_net,
    first_inserted_at,
    last_seen_at,
    last_updated_at
)
VALUES (
    %(room_revenue_night_data_key)s,
    %(tenant_key)s,
    %(enterprise_id)s,
    %(hotel_name)s,
    %(local_timezone)s,
    %(stay_date)s,
    %(amount_currency)s,
    %(room_revenue_excl_products_1_net)s,
    %(product_revenue_1_net)s,
    %(room_revenue_incl_products_1_net)s,
    now(),
    now(),
    now()
)
ON CONFLICT (room_revenue_night_data_key) DO UPDATE SET
    tenant_key = EXCLUDED.tenant_key,
    enterprise_id = EXCLUDED.enterprise_id,
    hotel_name = EXCLUDED.hotel_name,
    local_timezone = EXCLUDED.local_timezone,
    stay_date = EXCLUDED.stay_date,
    amount_currency = EXCLUDED.amount_currency,
    room_revenue_excl_products_1_net = EXCLUDED.room_revenue_excl_products_1_net,
    product_revenue_1_net = EXCLUDED.product_revenue_1_net,
    room_revenue_incl_products_1_net = EXCLUDED.room_revenue_incl_products_1_net,
    last_seen_at = now(),
    last_updated_at =
        CASE
            WHEN (
                functions.room_revenue_night_data.tenant_key,
                functions.room_revenue_night_data.enterprise_id,
                functions.room_revenue_night_data.hotel_name,
                functions.room_revenue_night_data.local_timezone,
                functions.room_revenue_night_data.stay_date,
                functions.room_revenue_night_data.amount_currency,
                functions.room_revenue_night_data.room_revenue_excl_products_1_net,
                functions.room_revenue_night_data.product_revenue_1_net,
                functions.room_revenue_night_data.room_revenue_incl_products_1_net
            ) IS DISTINCT FROM (
                EXCLUDED.tenant_key,
                EXCLUDED.enterprise_id,
                EXCLUDED.hotel_name,
                EXCLUDED.local_timezone,
                EXCLUDED.stay_date,
                EXCLUDED.amount_currency,
                EXCLUDED.room_revenue_excl_products_1_net,
                EXCLUDED.product_revenue_1_net,
                EXCLUDED.room_revenue_incl_products_1_net
            )
            THEN now()
            ELSE functions.room_revenue_night_data.last_updated_at
        END