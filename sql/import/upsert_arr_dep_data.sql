INSERT INTO functions.arr_dep_data (
    arr_dep_data_key,
    hotel_name,
    stay_date,
    total_arrivals,
    total_departures,
    first_inserted_at,
    last_seen_at,
    last_updated_at
)
VALUES (
    %(arr_dep_data_key)s,
    %(hotel_name)s,
    %(stay_date)s,
    %(total_arrivals)s,
    %(total_departures)s,
    now(),
    now(),
    now()
)
ON CONFLICT (arr_dep_data_key) DO UPDATE SET
    hotel_name = EXCLUDED.hotel_name,
    stay_date = EXCLUDED.stay_date,
    total_arrivals = EXCLUDED.total_arrivals,
    total_departures = EXCLUDED.total_departures,
    last_seen_at = now(),
    last_updated_at =
        CASE
            WHEN (
                functions.arr_dep_data.hotel_name,
                functions.arr_dep_data.stay_date,
                functions.arr_dep_data.total_arrivals,
                functions.arr_dep_data.total_departures
            ) IS DISTINCT FROM (
                EXCLUDED.hotel_name,
                EXCLUDED.stay_date,
                EXCLUDED.total_arrivals,
                EXCLUDED.total_departures
            )
            THEN now()
            ELSE functions.arr_dep_data.last_updated_at
        END