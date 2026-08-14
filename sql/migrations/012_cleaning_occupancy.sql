BEGIN;

-- Cleaning rows are now one per (room category, occupancy) rather than one per
-- guest-count band. A category serving 2 + 1 extra beds gets three rows, because
-- linen and cleaning minutes differ per occupancy.
--
-- The old UNIQUE (enterprise_id, category_name) allowed only a single row per
-- category name, which made per-occupancy rows impossible to store.

ALTER TABLE functions.cost_cleaning_categories
    ADD COLUMN IF NOT EXISTS resource_category_id text;

ALTER TABLE functions.cost_cleaning_categories
    ADD COLUMN IF NOT EXISTS occupancy integer;

-- Existing rows were guest bands; their lower bound is the closest equivalent.
UPDATE functions.cost_cleaning_categories
SET occupancy = greatest(coalesce(min_guests, 1), 1)
WHERE occupancy IS NULL;

ALTER TABLE functions.cost_cleaning_categories
    ALTER COLUMN occupancy SET NOT NULL;

DO $$
BEGIN
    ALTER TABLE functions.cost_cleaning_categories
        DROP CONSTRAINT IF EXISTS cost_cleaning_categories_enterprise_id_category_name_key;
EXCEPTION WHEN undefined_object THEN
    NULL;
END $$;

ALTER TABLE functions.cost_cleaning_categories
    DROP CONSTRAINT IF EXISTS cost_cleaning_categories_occupancy_check;
ALTER TABLE functions.cost_cleaning_categories
    ADD CONSTRAINT cost_cleaning_categories_occupancy_check CHECK (occupancy >= 1);

CREATE UNIQUE INDEX IF NOT EXISTS ux_cost_cleaning_categories_occupancy
ON functions.cost_cleaning_categories (enterprise_id, category_name, occupancy);

CREATE INDEX IF NOT EXISTS ix_cost_cleaning_categories_resource_category
ON functions.cost_cleaning_categories (enterprise_id, resource_category_id);

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('012_cleaning_occupancy')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
