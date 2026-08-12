BEGIN;

/*
 * Migrates both earlier cost-settings layouts:
 *   1. hotel_name was the primary/foreign key;
 *   2. enterprise_id existed as uuid.
 *
 * Existing property settings are preserved. Hotel-name keyed rows are matched
 * to enterprise IDs already imported into functions.* fact tables. A legacy
 * key is retained for an unmatched row so this migration never discards it.
 */

ALTER TABLE IF EXISTS functions.cost_property_settings
    ADD COLUMN IF NOT EXISTS enterprise_id text;

ALTER TABLE IF EXISTS functions.cost_property_settings
    ADD COLUMN IF NOT EXISTS breakfast_calculation_basis text NOT NULL DEFAULT 'guests';

ALTER TABLE IF EXISTS functions.cost_distribution_groups
    ADD COLUMN IF NOT EXISTS enterprise_id text;
ALTER TABLE IF EXISTS functions.cost_cleaning_categories
    ADD COLUMN IF NOT EXISTS enterprise_id text;
ALTER TABLE IF EXISTS functions.cost_arrival_staffing_tiers
    ADD COLUMN IF NOT EXISTS enterprise_id text;
ALTER TABLE IF EXISTS functions.cost_breakfast_staffing_tiers
    ADD COLUMN IF NOT EXISTS enterprise_id text;
ALTER TABLE IF EXISTS functions.cost_fixed_lines
    ADD COLUMN IF NOT EXISTS enterprise_id text;

/* Resolve existing hotel-name settings against imported source identifiers. */
WITH property_candidates AS (
    SELECT enterprise_id::text, trim(hotel_name) AS hotel_name, 1 AS priority
    FROM functions.breakfast_data
    WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL

    UNION ALL

    SELECT enterprise_id::text, trim(hotel_name), 2
    FROM functions.parking_data
    WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL

    UNION ALL

    SELECT enterprise_id::text, trim(hotel_name), 3
    FROM functions.total_payment_data
    WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL

    UNION ALL

    SELECT enterprise_id::text, trim(hotel_name), 4
    FROM functions.room_revenue_night_data
    WHERE enterprise_id IS NOT NULL AND nullif(trim(hotel_name), '') IS NOT NULL
),
property_map AS (
    SELECT DISTINCT ON (lower(hotel_name))
        lower(hotel_name) AS normalized_name,
        enterprise_id
    FROM property_candidates
    ORDER BY lower(hotel_name), priority, enterprise_id
)
UPDATE functions.cost_property_settings AS settings
SET enterprise_id = property_map.enterprise_id
FROM property_map
WHERE settings.enterprise_id IS NULL
  AND lower(trim(settings.hotel_name)) = property_map.normalized_name;

UPDATE functions.cost_property_settings
SET enterprise_id = 'legacy:' || md5(lower(trim(hotel_name)))
WHERE enterprise_id IS NULL;

/* Copy the resolved parent key into hotel-name keyed child tables when present. */
DO $migration$
DECLARE
    target_table text;
BEGIN
    FOREACH target_table IN ARRAY ARRAY[
        'cost_distribution_groups',
        'cost_cleaning_categories',
        'cost_arrival_staffing_tiers',
        'cost_breakfast_staffing_tiers',
        'cost_fixed_lines'
    ]
    LOOP
        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'functions'
              AND information_schema.columns.table_name = target_table
              AND column_name = 'hotel_name'
        ) THEN
            EXECUTE format(
                'UPDATE functions.%I AS child '
                'SET enterprise_id = parent.enterprise_id '
                'FROM functions.cost_property_settings AS parent '
                'WHERE child.enterprise_id IS NULL '
                'AND lower(trim(child.hotel_name)) = lower(trim(parent.hotel_name))',
                target_table
            );
        END IF;
    END LOOP;
END
$migration$;

/* Drop foreign keys before changing uuid columns to opaque text identifiers. */
DO $migration$
DECLARE
    constraint_record record;
BEGIN
    FOR constraint_record IN
        SELECT conrelid::regclass AS table_name, conname
        FROM pg_constraint
        WHERE contype = 'f'
          AND confrelid = 'functions.cost_property_settings'::regclass
    LOOP
        EXECUTE format(
            'ALTER TABLE %s DROP CONSTRAINT %I',
            constraint_record.table_name,
            constraint_record.conname
        );
    END LOOP;
END
$migration$;

DO $migration$
DECLARE
    primary_key_name text;
BEGIN
    SELECT conname INTO primary_key_name
    FROM pg_constraint
    WHERE conrelid = 'functions.cost_property_settings'::regclass
      AND contype = 'p';

    IF primary_key_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE functions.cost_property_settings DROP CONSTRAINT %I',
            primary_key_name
        );
    END IF;
END
$migration$;

ALTER TABLE functions.cost_property_settings
    ALTER COLUMN enterprise_id TYPE text USING enterprise_id::text;
ALTER TABLE functions.cost_distribution_groups
    ALTER COLUMN enterprise_id TYPE text USING enterprise_id::text;
ALTER TABLE functions.cost_cleaning_categories
    ALTER COLUMN enterprise_id TYPE text USING enterprise_id::text;
ALTER TABLE functions.cost_arrival_staffing_tiers
    ALTER COLUMN enterprise_id TYPE text USING enterprise_id::text;
ALTER TABLE functions.cost_breakfast_staffing_tiers
    ALTER COLUMN enterprise_id TYPE text USING enterprise_id::text;
ALTER TABLE functions.cost_fixed_lines
    ALTER COLUMN enterprise_id TYPE text USING enterprise_id::text;

ALTER TABLE functions.cost_property_settings ALTER COLUMN enterprise_id SET NOT NULL;
ALTER TABLE functions.cost_distribution_groups ALTER COLUMN enterprise_id SET NOT NULL;
ALTER TABLE functions.cost_cleaning_categories ALTER COLUMN enterprise_id SET NOT NULL;
ALTER TABLE functions.cost_arrival_staffing_tiers ALTER COLUMN enterprise_id SET NOT NULL;
ALTER TABLE functions.cost_breakfast_staffing_tiers ALTER COLUMN enterprise_id SET NOT NULL;
ALTER TABLE functions.cost_fixed_lines ALTER COLUMN enterprise_id SET NOT NULL;

/* Old child hotel_name columns may remain for auditability, but are no longer keys. */
DO $migration$
DECLARE
    target_table text;
BEGIN
    FOREACH target_table IN ARRAY ARRAY[
        'cost_distribution_groups',
        'cost_cleaning_categories',
        'cost_arrival_staffing_tiers',
        'cost_breakfast_staffing_tiers',
        'cost_fixed_lines'
    ]
    LOOP
        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'functions'
              AND information_schema.columns.table_name = target_table
              AND column_name = 'hotel_name'
        ) THEN
            EXECUTE format(
                'ALTER TABLE functions.%I ALTER COLUMN hotel_name DROP NOT NULL',
                target_table
            );
        END IF;
    END LOOP;
END
$migration$;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'functions.cost_property_settings'::regclass
          AND conname = 'cost_property_settings_breakfast_basis_check'
    ) THEN
        ALTER TABLE functions.cost_property_settings
            ADD CONSTRAINT cost_property_settings_breakfast_basis_check
            CHECK (breakfast_calculation_basis IN ('guests', 'products'));
    END IF;
END
$migration$;

ALTER TABLE functions.cost_property_settings
    ADD CONSTRAINT cost_property_settings_pkey PRIMARY KEY (enterprise_id);

ALTER TABLE functions.cost_distribution_groups
    ADD CONSTRAINT cost_distribution_groups_enterprise_id_fkey
    FOREIGN KEY (enterprise_id)
    REFERENCES functions.cost_property_settings(enterprise_id) ON DELETE CASCADE;
ALTER TABLE functions.cost_cleaning_categories
    ADD CONSTRAINT cost_cleaning_categories_enterprise_id_fkey
    FOREIGN KEY (enterprise_id)
    REFERENCES functions.cost_property_settings(enterprise_id) ON DELETE CASCADE;
ALTER TABLE functions.cost_arrival_staffing_tiers
    ADD CONSTRAINT cost_arrival_staffing_tiers_enterprise_id_fkey
    FOREIGN KEY (enterprise_id)
    REFERENCES functions.cost_property_settings(enterprise_id) ON DELETE CASCADE;
ALTER TABLE functions.cost_breakfast_staffing_tiers
    ADD CONSTRAINT cost_breakfast_staffing_tiers_enterprise_id_fkey
    FOREIGN KEY (enterprise_id)
    REFERENCES functions.cost_property_settings(enterprise_id) ON DELETE CASCADE;
ALTER TABLE functions.cost_fixed_lines
    ADD CONSTRAINT cost_fixed_lines_enterprise_id_fkey
    FOREIGN KEY (enterprise_id)
    REFERENCES functions.cost_property_settings(enterprise_id) ON DELETE CASCADE;

CREATE UNIQUE INDEX IF NOT EXISTS ux_cost_distribution_groups_enterprise_name
    ON functions.cost_distribution_groups(enterprise_id, group_name);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cost_cleaning_categories_enterprise_name
    ON functions.cost_cleaning_categories(enterprise_id, category_name);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cost_fixed_lines_enterprise_name
    ON functions.cost_fixed_lines(enterprise_id, cost_name);

CREATE INDEX IF NOT EXISTS ix_cost_property_settings_hotel_name
    ON functions.cost_property_settings(hotel_name);
CREATE INDEX IF NOT EXISTS ix_cost_distribution_groups_enterprise
    ON functions.cost_distribution_groups(enterprise_id);
CREATE INDEX IF NOT EXISTS ix_cost_cleaning_categories_enterprise
    ON functions.cost_cleaning_categories(enterprise_id);
CREATE INDEX IF NOT EXISTS ix_cost_arrival_tiers_enterprise
    ON functions.cost_arrival_staffing_tiers(enterprise_id);
CREATE INDEX IF NOT EXISTS ix_cost_breakfast_tiers_enterprise
    ON functions.cost_breakfast_staffing_tiers(enterprise_id);
CREATE INDEX IF NOT EXISTS ix_cost_fixed_lines_enterprise
    ON functions.cost_fixed_lines(enterprise_id);

COMMIT;
