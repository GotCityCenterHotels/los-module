CREATE SCHEMA IF NOT EXISTS functions;

CREATE TABLE IF NOT EXISTS functions.breakfast_data (
    breakfast_data_key text PRIMARY KEY,

    enterprise_id uuid NOT NULL,
    hotel_name text NOT NULL,
    stay_date date NOT NULL,

    breakfast_total integer NOT NULL,
    breakfast_net_cost numeric(18, 2),

    first_inserted_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_breakfast_data_enterprise_id
ON functions.breakfast_data (enterprise_id);

CREATE INDEX IF NOT EXISTS ix_breakfast_data_hotel_name
ON functions.breakfast_data (hotel_name);

CREATE INDEX IF NOT EXISTS ix_breakfast_data_stay_date
ON functions.breakfast_data (stay_date);