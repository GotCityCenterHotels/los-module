INSERT INTO functions.breakfast_data (
    breakfast_data_key,
    enterprise_id,
    hotel_name,
    stay_date,
    breakfast_total,
    breakfast_net_cost,
    first_inserted_at,
    last_seen_at,
    last_updated_at
)
VALUES (
    %(breakfast_data_key)s,
    %(enterprise_id)s,
    %(hotel_name)s,
    %(stay_date)s,
    %(breakfast_total)s,
    %(breakfast_net_cost)s,
    now(),
    now(),
    now()
)
ON CONFLICT (breakfast_data_key) DO UPDATE SET
    enterprise_id = EXCLUDED.enterprise_id,
    hotel_name = EXCLUDED.hotel_name,
    stay_date = EXCLUDED.stay_date,
    breakfast_total = EXCLUDED.breakfast_total,
    breakfast_net_cost = EXCLUDED.breakfast_net_cost,
    last_seen_at = now(),
    last_updated_at =
        CASE
            WHEN (
                functions.breakfast_data.enterprise_id,
                functions.breakfast_data.hotel_name,
                functions.breakfast_data.stay_date,
                functions.breakfast_data.breakfast_total,
                functions.breakfast_data.breakfast_net_cost
            ) IS DISTINCT FROM (
                EXCLUDED.enterprise_id,
                EXCLUDED.hotel_name,
                EXCLUDED.stay_date,
                EXCLUDED.breakfast_total,
                EXCLUDED.breakfast_net_cost
            )
            THEN now()
            ELSE functions.breakfast_data.last_updated_at
        END