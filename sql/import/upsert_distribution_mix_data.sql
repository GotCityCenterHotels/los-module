INSERT INTO functions.distribution_mix_data (
    distribution_mix_data_key,
    enterprise_id,
    hotel_name,
    stay_date,
    origin,
    travel_agency,
    rate_name,
    room_revenue_net,
    reservation_count,
    first_inserted_at,
    last_seen_at,
    last_updated_at
)
VALUES (
    %(distribution_mix_data_key)s,
    %(enterprise_id)s,
    %(hotel_name)s,
    %(stay_date)s,
    %(origin)s,
    %(travel_agency)s,
    %(rate_name)s,
    %(room_revenue_net)s,
    %(reservation_count)s,
    now(),
    now(),
    now()
)
ON CONFLICT (distribution_mix_data_key) DO UPDATE SET
    enterprise_id = EXCLUDED.enterprise_id,
    hotel_name = EXCLUDED.hotel_name,
    stay_date = EXCLUDED.stay_date,
    origin = EXCLUDED.origin,
    travel_agency = EXCLUDED.travel_agency,
    rate_name = EXCLUDED.rate_name,
    room_revenue_net = EXCLUDED.room_revenue_net,
    reservation_count = EXCLUDED.reservation_count,
    -- See upsert_departure_mix_data.sql: an (origin, agency, rate) combination
    -- that stops occurring on a day has no row here to overwrite, so the
    -- importer prunes on this column instead.
    last_seen_at = now(),
    last_updated_at =
        CASE
            WHEN (
                functions.distribution_mix_data.enterprise_id,
                functions.distribution_mix_data.hotel_name,
                functions.distribution_mix_data.stay_date,
                functions.distribution_mix_data.origin,
                functions.distribution_mix_data.travel_agency,
                functions.distribution_mix_data.rate_name,
                functions.distribution_mix_data.room_revenue_net,
                functions.distribution_mix_data.reservation_count
            ) IS DISTINCT FROM (
                EXCLUDED.enterprise_id,
                EXCLUDED.hotel_name,
                EXCLUDED.stay_date,
                EXCLUDED.origin,
                EXCLUDED.travel_agency,
                EXCLUDED.rate_name,
                EXCLUDED.room_revenue_net,
                EXCLUDED.reservation_count
            )
            THEN now()
            ELSE functions.distribution_mix_data.last_updated_at
        END
