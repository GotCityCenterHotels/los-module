BEGIN;

CREATE TABLE IF NOT EXISTS functions.cost_properties (
    enterprise_id text PRIMARY KEY,
    tenant_key text NOT NULL,
    hotel_name text NOT NULL,
    first_inserted_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (nullif(trim(enterprise_id), '') IS NOT NULL),
    CHECK (nullif(trim(hotel_name), '') IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_cost_properties_hotel_name
ON functions.cost_properties (hotel_name);

CREATE TABLE IF NOT EXISTS functions.schema_migrations (
    migration_name text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('002_cost_properties')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
