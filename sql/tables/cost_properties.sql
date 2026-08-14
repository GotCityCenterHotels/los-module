CREATE SCHEMA IF NOT EXISTS functions;

CREATE TABLE IF NOT EXISTS functions.hotels (
    enterprise_id text PRIMARY KEY,
    tenant_key text NOT NULL,
    hotel_name text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (nullif(trim(enterprise_id), '') IS NOT NULL),
    CHECK (nullif(trim(hotel_name), '') IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_hotels_tenant_active_name
ON functions.hotels (tenant_key, active, hotel_name, enterprise_id);
