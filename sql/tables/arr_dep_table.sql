CREATE SCHEMA IF NOT EXISTS functions;

CREATE TABLE IF NOT EXISTS functions.arr_dep_data (
    arr_dep_data_key text PRIMARY KEY,

    hotel_name text NOT NULL,
    stay_date date NOT NULL,

    total_arrivals integer NOT NULL,
    total_departures integer NOT NULL,

    first_inserted_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_arr_dep_data_hotel_name
ON functions.arr_dep_data (hotel_name);

CREATE INDEX IF NOT EXISTS ix_arr_dep_data_event_date
ON functions.arr_dep_data (stay_date);