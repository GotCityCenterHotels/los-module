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

/* Seed the mirror from already imported facts. The timer refresh replaces
 * this best-effort seed with the authoritative enterprise_current values. */
WITH candidates AS (
    SELECT enterprise_id::text, trim(hotel_name)::text AS hotel_name, 1 AS priority
    FROM functions.breakfast_data
    WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL

    UNION ALL

    SELECT enterprise_id::text, trim(hotel_name)::text, 2
    FROM functions.parking_data
    WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL

    UNION ALL

    SELECT enterprise_id::text, trim(hotel_name)::text, 3
    FROM functions.total_payment_data
    WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL

    UNION ALL

    SELECT enterprise_id::text, trim(hotel_name)::text, 4
    FROM functions.room_revenue_night_data
    WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL
),
properties AS (
    SELECT DISTINCT ON (enterprise_id)
        enterprise_id,
        hotel_name
    FROM candidates
    ORDER BY enterprise_id, priority, hotel_name
)
INSERT INTO functions.cost_properties (enterprise_id, tenant_key, hotel_name)
SELECT enterprise_id, 'GCCH', hotel_name
FROM properties
ON CONFLICT (enterprise_id) DO UPDATE SET
    hotel_name = EXCLUDED.hotel_name,
    last_seen_at = now(),
    last_updated_at = CASE
        WHEN functions.cost_properties.hotel_name
            IS DISTINCT FROM EXCLUDED.hotel_name
        THEN now()
        ELSE functions.cost_properties.last_updated_at
    END;

CREATE TABLE IF NOT EXISTS functions.schema_migrations (
    migration_name text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('002_cost_properties')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
