BEGIN;

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
    CHECK (nullif(trim(tenant_key), '') IS NOT NULL),
    CHECK (nullif(trim(hotel_name), '') IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_hotels_tenant_active_name
    ON functions.hotels (tenant_key, active, hotel_name, enterprise_id);

/* Copy every legacy dimension before changing dependencies or dropping it. */
DO $migration$
BEGIN
    IF to_regclass('functions.cost_properties') IS NOT NULL THEN
        INSERT INTO functions.hotels (
            enterprise_id, tenant_key, hotel_name,
            first_seen_at, last_seen_at, last_updated_at
        )
        SELECT enterprise_id::text, tenant_key, trim(hotel_name),
               first_inserted_at, last_seen_at, last_updated_at
        FROM functions.cost_properties
        ON CONFLICT (enterprise_id) DO UPDATE SET
            tenant_key = EXCLUDED.tenant_key,
            hotel_name = EXCLUDED.hotel_name,
            active = true,
            first_seen_at = least(functions.hotels.first_seen_at, EXCLUDED.first_seen_at),
            last_seen_at = greatest(functions.hotels.last_seen_at, EXCLUDED.last_seen_at),
            last_updated_at = greatest(
                functions.hotels.last_updated_at, EXCLUDED.last_updated_at
            );
    END IF;

    IF to_regclass('functions.supplement_hotels') IS NOT NULL THEN
        INSERT INTO functions.hotels (
            enterprise_id, tenant_key, hotel_name, active, last_seen_at
        )
        SELECT enterprise_id::text, tenant_key, trim(hotel_name), active, last_seen_at
        FROM functions.supplement_hotels
        ON CONFLICT (enterprise_id) DO UPDATE SET
            tenant_key = EXCLUDED.tenant_key,
            hotel_name = EXCLUDED.hotel_name,
            active = functions.hotels.active OR EXCLUDED.active,
            last_seen_at = greatest(functions.hotels.last_seen_at, EXCLUDED.last_seen_at),
            last_updated_at = CASE
                WHEN functions.hotels.hotel_name IS DISTINCT FROM EXCLUDED.hotel_name
                THEN now() ELSE functions.hotels.last_updated_at
            END;
    END IF;

    IF to_regclass('functions.cost_property_settings') IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'functions'
             AND table_name = 'cost_property_settings'
             AND column_name = 'hotel_name'
       ) THEN
        INSERT INTO functions.hotels (enterprise_id, tenant_key, hotel_name)
        SELECT enterprise_id::text, 'GCCH', trim(hotel_name)
        FROM functions.cost_property_settings
        WHERE nullif(trim(hotel_name), '') IS NOT NULL
        ON CONFLICT (enterprise_id) DO NOTHING;
    END IF;
END
$migration$;

/* Normalize all fact identifiers so they can reference the shared text key. */
ALTER TABLE IF EXISTS functions.breakfast_data
    ALTER COLUMN enterprise_id TYPE text USING enterprise_id::text;
ALTER TABLE IF EXISTS functions.parking_data
    ALTER COLUMN enterprise_id TYPE text USING enterprise_id::text;
ALTER TABLE IF EXISTS functions.total_payment_data
    ALTER COLUMN enterprise_id TYPE text USING enterprise_id::text;

/* Arrivals/departures was the only cost fact without a stable hotel ID. */
ALTER TABLE IF EXISTS functions.arr_dep_data
    ADD COLUMN IF NOT EXISTS enterprise_id text;

DO $migration$
DECLARE
    missing_count bigint;
BEGIN
    IF to_regclass('functions.arr_dep_data') IS NOT NULL THEN
        UPDATE functions.arr_dep_data AS fact
        SET enterprise_id = hotel.enterprise_id
        FROM functions.hotels AS hotel
        WHERE fact.enterprise_id IS NULL
          AND hotel.tenant_key = 'GCCH'
          AND trim(hotel.hotel_name) = trim(fact.hotel_name);

        SELECT count(*) INTO missing_count
        FROM functions.arr_dep_data
        WHERE enterprise_id IS NULL;
        IF missing_count > 0 THEN
            RAISE EXCEPTION
                'Cannot unify hotels: % arrivals/departures rows have no enterprise mapping',
                missing_count;
        END IF;
        ALTER TABLE functions.arr_dep_data ALTER COLUMN enterprise_id SET NOT NULL;
        UPDATE functions.arr_dep_data
        SET arr_dep_data_key = md5(
            concat_ws('|', enterprise_id, stay_date::text)
        );
    END IF;
END
$migration$;

/* Fail before cleanup if any fact/settings identity is absent from the dimension. */
DO $migration$
DECLARE
    target_table text;
    missing_count bigint;
BEGIN
    FOREACH target_table IN ARRAY ARRAY[
        'cost_property_settings',
        'breakfast_data',
        'parking_data',
        'room_revenue_night_data',
        'total_payment_data',
        'arr_dep_data'
    ]
    LOOP
        IF to_regclass('functions.' || target_table) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format(
            'SELECT count(*) FROM functions.%I source '
            'WHERE NOT EXISTS ('
            'SELECT 1 FROM functions.hotels hotel '
            'WHERE hotel.enterprise_id = source.enterprise_id::text)',
            target_table
        ) INTO missing_count;
        IF missing_count > 0 THEN
            RAISE EXCEPTION
                'Cannot unify hotels: % rows in functions.% have no hotel dimension row',
                missing_count, target_table;
        END IF;
    END LOOP;
END
$migration$;

/* Move live foreign keys to the unified dimension. */
ALTER TABLE IF EXISTS functions.supplement_room_categories
    DROP CONSTRAINT IF EXISTS supplement_room_categories_hotel_code_fkey;
ALTER TABLE IF EXISTS functions.supplement_room_categories
    ADD CONSTRAINT supplement_room_categories_hotel_code_fkey
    FOREIGN KEY (hotel_code) REFERENCES functions.hotels(enterprise_id);

ALTER TABLE IF EXISTS functions.cost_property_settings
    DROP CONSTRAINT IF EXISTS cost_property_settings_hotel_fkey;
ALTER TABLE IF EXISTS functions.cost_property_settings
    ADD CONSTRAINT cost_property_settings_hotel_fkey
    FOREIGN KEY (enterprise_id) REFERENCES functions.hotels(enterprise_id);

ALTER TABLE IF EXISTS functions.breakfast_data
    DROP CONSTRAINT IF EXISTS breakfast_data_hotel_fkey;
ALTER TABLE IF EXISTS functions.breakfast_data
    ADD CONSTRAINT breakfast_data_hotel_fkey
    FOREIGN KEY (enterprise_id) REFERENCES functions.hotels(enterprise_id);
ALTER TABLE IF EXISTS functions.parking_data
    DROP CONSTRAINT IF EXISTS parking_data_hotel_fkey;
ALTER TABLE IF EXISTS functions.parking_data
    ADD CONSTRAINT parking_data_hotel_fkey
    FOREIGN KEY (enterprise_id) REFERENCES functions.hotels(enterprise_id);
ALTER TABLE IF EXISTS functions.room_revenue_night_data
    DROP CONSTRAINT IF EXISTS room_revenue_night_data_hotel_fkey;
ALTER TABLE IF EXISTS functions.room_revenue_night_data
    ADD CONSTRAINT room_revenue_night_data_hotel_fkey
    FOREIGN KEY (enterprise_id) REFERENCES functions.hotels(enterprise_id);
ALTER TABLE IF EXISTS functions.total_payment_data
    DROP CONSTRAINT IF EXISTS total_payment_data_hotel_fkey;
ALTER TABLE IF EXISTS functions.total_payment_data
    ADD CONSTRAINT total_payment_data_hotel_fkey
    FOREIGN KEY (enterprise_id) REFERENCES functions.hotels(enterprise_id);
ALTER TABLE IF EXISTS functions.arr_dep_data
    DROP CONSTRAINT IF EXISTS arr_dep_data_hotel_fkey;
ALTER TABLE IF EXISTS functions.arr_dep_data
    ADD CONSTRAINT arr_dep_data_hotel_fkey
    FOREIGN KEY (enterprise_id) REFERENCES functions.hotels(enterprise_id);

/*
 * Keep the legacy name column nullable during a rolling deployment. New code
 * reads the name from functions.hotels; migration 007 removes this column and
 * the two legacy hotel tables after all Function instances run the new code.
 */
DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'functions'
          AND table_name = 'cost_property_settings'
          AND column_name = 'hotel_name'
    ) THEN
        ALTER TABLE functions.cost_property_settings
            ALTER COLUMN hotel_name DROP NOT NULL;
    END IF;
END
$migration$;

DO $migration$
BEGIN
    IF to_regclass('functions.arr_dep_data') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS ix_arr_dep_data_enterprise_stay_date
            ON functions.arr_dep_data (enterprise_id, stay_date);
    END IF;
END
$migration$;

CREATE TABLE IF NOT EXISTS functions.schema_migrations (
    migration_name text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('006_unified_hotels')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
