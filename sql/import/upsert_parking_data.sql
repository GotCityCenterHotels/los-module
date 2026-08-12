INSERT INTO functions.parking_data (
    parking_data_key,
    enterprise_id,
    hotel_name,
    service,
    stay_date,
    total_reservations_using_parking,
    total_parking_spots,
    total_parking_amount_net_value,
    first_inserted_at,
    last_seen_at,
    last_updated_at
)
VALUES (
    %(parking_data_key)s,
    %(enterprise_id)s,
    %(hotel_name)s,
    %(service)s,
    %(stay_date)s,
    %(total_reservations_using_parking)s,
    %(total_parking_spots)s,
    %(total_parking_amount_net_value)s,
    now(),
    now(),
    now()
)
ON CONFLICT (parking_data_key) DO UPDATE SET
    enterprise_id = EXCLUDED.enterprise_id,
    hotel_name = EXCLUDED.hotel_name,
    service = EXCLUDED.service,
    stay_date = EXCLUDED.stay_date,
    total_reservations_using_parking = EXCLUDED.total_reservations_using_parking,
    total_parking_spots = EXCLUDED.total_parking_spots,
    total_parking_amount_net_value = EXCLUDED.total_parking_amount_net_value,
    last_seen_at = now(),
    last_updated_at =
        CASE
            WHEN (
                functions.parking_data.enterprise_id,
                functions.parking_data.hotel_name,
                functions.parking_data.service,
                functions.parking_data.stay_date,
                functions.parking_data.total_reservations_using_parking,
                functions.parking_data.total_parking_spots,
                functions.parking_data.total_parking_amount_net_value
            ) IS DISTINCT FROM (
                EXCLUDED.enterprise_id,
                EXCLUDED.hotel_name,
                EXCLUDED.service,
                EXCLUDED.stay_date,
                EXCLUDED.total_reservations_using_parking,
                EXCLUDED.total_parking_spots,
                EXCLUDED.total_parking_amount_net_value
            )
            THEN now()
            ELSE functions.parking_data.last_updated_at
        END