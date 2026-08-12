CREATE TABLE IF NOT EXISTS functions.room_revenue_night_data (
    room_revenue_night_data_key text PRIMARY KEY,

    tenant_key text NOT NULL,
    enterprise_id text NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_room_revenue_night_data_enterprise_stay_date
ON functions.room_revenue_night_data (enterprise_id, stay_date);

CREATE INDEX IF NOT EXISTS idx_room_revenue_night_data_tenant_stay_date
ON functions.room_revenue_night_data (tenant_key, stay_date);

CREATE INDEX IF NOT EXISTS idx_room_revenue_night_data_last_seen_at
ON functions.room_revenue_night_data (last_seen_at);

