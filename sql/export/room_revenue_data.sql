SELECT
    md5(
        concat_ws(
            '|',
            tenant_key,
            enterprise_id::text,
            stay_date::text,
            coalesce(amount_currency, '')
        )
    ) AS room_revenue_night_data_key,

    tenant_key,
    enterprise_id::text AS enterprise_id,
    hotel_name,
    local_timezone,
    stay_date,
    amount_currency,
    room_revenue_excl_products_1_net,
    product_revenue_1_net,
    room_revenue_incl_products_1_net
FROM reporting.room_revenue_night_current;