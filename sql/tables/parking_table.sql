CREATE SCHEMA IF NOT EXISTS functions;

CREATE TABLE IF NOT EXISTS functions.parking_data (
    parking_data_key text PRIMARY KEY,

    enterprise_id text NOT NULL
        CONSTRAINT parking_data_hotel_fkey
        REFERENCES functions.hotels(enterprise_id),
    hotel_name text,
    service text,
    stay_date date,

    total_reservations_using_parking integer,
    total_parking_spots integer,
    total_parking_amount_net_value numeric(18, 2),

    first_inserted_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_parking_data_enterprise_id
ON functions.parking_data (enterprise_id);

CREATE INDEX IF NOT EXISTS ix_parking_data_hotel_name
ON functions.parking_data (hotel_name);

CREATE INDEX IF NOT EXISTS ix_parking_data_stay_date
ON functions.parking_data (stay_date);

