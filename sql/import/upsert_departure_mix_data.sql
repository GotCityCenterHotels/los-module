INSERT INTO functions.departure_mix_data (
    departure_mix_data_key,
    enterprise_id,
    hotel_name,
    stay_date,
    resource_category_id,
    category_name,
    occupancy,
    allocated_cleanings,
    first_inserted_at,
    last_seen_at,
    last_updated_at
)
VALUES (
    %(departure_mix_data_key)s,
    %(enterprise_id)s,
    %(hotel_name)s,
    %(stay_date)s,
    %(resource_category_id)s,
    %(category_name)s,
    %(occupancy)s,
    %(allocated_cleanings)s,
    now(),
    now(),
    now()
)
ON CONFLICT (departure_mix_data_key) DO UPDATE SET
    enterprise_id = EXCLUDED.enterprise_id,
    hotel_name = EXCLUDED.hotel_name,
    stay_date = EXCLUDED.stay_date,
    resource_category_id = EXCLUDED.resource_category_id,
    category_name = EXCLUDED.category_name,
    occupancy = EXCLUDED.occupancy,
    allocated_cleanings = EXCLUDED.allocated_cleanings,
    -- Stamped on every touch, seen or unseen. The importer prunes on it: a
    -- (category, occupancy) that no longer has departures on a day has no row
    -- here to overwrite, so it would otherwise keep its old count for good and
    -- quietly skew that day's mix.
    last_seen_at = now(),
    last_updated_at =
        CASE
            WHEN (
                functions.departure_mix_data.enterprise_id,
                functions.departure_mix_data.hotel_name,
                functions.departure_mix_data.stay_date,
                functions.departure_mix_data.resource_category_id,
                functions.departure_mix_data.category_name,
                functions.departure_mix_data.occupancy,
                functions.departure_mix_data.allocated_cleanings
            ) IS DISTINCT FROM (
                EXCLUDED.enterprise_id,
                EXCLUDED.hotel_name,
                EXCLUDED.stay_date,
                EXCLUDED.resource_category_id,
                EXCLUDED.category_name,
                EXCLUDED.occupancy,
                EXCLUDED.allocated_cleanings
            )
            THEN now()
            ELSE functions.departure_mix_data.last_updated_at
        END
