BEGIN;

-- The five cost fact tables previously existed only as unreferenced files in
-- sql/tables/. Nothing in the application ever applied them, so a rebuilt
-- Database A could not run five of the six cost datasets. These statements are
-- IF NOT EXISTS so an existing database keeps its current tables untouched and
-- only gains the indexes below.

CREATE TABLE IF NOT EXISTS functions.arr_dep_data (
    arr_dep_data_key text PRIMARY KEY,
    enterprise_id text NOT NULL
        CONSTRAINT arr_dep_data_hotel_fkey
        REFERENCES functions.hotels(enterprise_id),
    hotel_name text NOT NULL,
    stay_date date NOT NULL,
    total_arrivals integer NOT NULL,
    total_departures integer NOT NULL,
    first_inserted_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS functions.breakfast_data (
    breakfast_data_key text PRIMARY KEY,
    enterprise_id text NOT NULL
        CONSTRAINT breakfast_data_hotel_fkey
        REFERENCES functions.hotels(enterprise_id),
    hotel_name text NOT NULL,
    stay_date date NOT NULL,
    breakfast_total integer NOT NULL,
    breakfast_net_cost numeric(18, 2),
    first_inserted_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now()
);

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

CREATE TABLE IF NOT EXISTS functions.room_revenue_night_data (
    room_revenue_night_data_key text PRIMARY KEY,
    tenant_key text NOT NULL,
    enterprise_id text NOT NULL
        CONSTRAINT room_revenue_night_data_hotel_fkey
        REFERENCES functions.hotels(enterprise_id),
    hotel_name text,
    local_timezone text,
    stay_date date NOT NULL,
    amount_currency text,
    room_revenue_excl_products_1_net numeric,
    product_revenue_1_net numeric,
    room_revenue_incl_products_1_net numeric,
    first_inserted_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS functions.total_payment_data (
    total_payment_data_key text PRIMARY KEY,
    enterprise_id text NOT NULL
        CONSTRAINT total_payment_data_hotel_fkey
        REFERENCES functions.hotels(enterprise_id),
    hotel_name text,
    stay_date date NOT NULL,
    amount_currency text NOT NULL,
    total_payment_amount_gross_value numeric(18, 2),
    first_inserted_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now()
);

-- Read-path indexes.
--
-- Every query in queries/cost_data.py filters "stay_date BETWEEN %(start)s AND
-- %(end)s" with no enterprise predicate, then joins functions.hotels on
-- enterprise_id. Leading with stay_date lets the range drive the scan and keeps
-- enterprise_id available for the join without a heap lookup. The existing
-- (enterprise_id, stay_date) indexes cannot serve that shape because the range
-- column is not the leading key.

CREATE INDEX IF NOT EXISTS ix_arr_dep_data_stay_date_enterprise
ON functions.arr_dep_data (stay_date, enterprise_id);

CREATE INDEX IF NOT EXISTS ix_breakfast_data_stay_date_enterprise
ON functions.breakfast_data (stay_date, enterprise_id);

CREATE INDEX IF NOT EXISTS ix_parking_data_stay_date_enterprise
ON functions.parking_data (stay_date, enterprise_id);

CREATE INDEX IF NOT EXISTS ix_room_revenue_night_data_stay_date_enterprise
ON functions.room_revenue_night_data (stay_date, enterprise_id);

CREATE INDEX IF NOT EXISTS ix_total_payment_data_stay_date_enterprise
ON functions.total_payment_data (stay_date, enterprise_id);

-- Import-path indexes: upserts match on the surrogate key (already the primary
-- key), but the property picker's fallback scans these tables for distinct
-- enterprise_id values.
CREATE INDEX IF NOT EXISTS ix_arr_dep_data_enterprise_stay_date
ON functions.arr_dep_data (enterprise_id, stay_date);

CREATE INDEX IF NOT EXISTS ix_breakfast_data_enterprise_id
ON functions.breakfast_data (enterprise_id);

CREATE INDEX IF NOT EXISTS ix_parking_data_enterprise_id
ON functions.parking_data (enterprise_id);

CREATE INDEX IF NOT EXISTS ix_room_revenue_night_data_enterprise_stay_date
ON functions.room_revenue_night_data (enterprise_id, stay_date);

CREATE INDEX IF NOT EXISTS ix_total_payment_data_enterprise_id
ON functions.total_payment_data (enterprise_id);

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('010_cost_fact_tables')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
