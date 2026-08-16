BEGIN;

-- Cleaning is re-cut around bed types.
--
-- Linen cost used to be a number typed into every (category, occupancy) row -
-- one number per row, with nothing saying where it came from and no way to
-- change "a double bed's linen costs 75 kr" in one place. It is now a property
-- of the bed: a property defines its bed types once, a room category's rows say
-- which beds are made up, and the linen cost follows.
--
-- The other half is inheritance. Most categories have one bed setup whatever
-- the occupancy, so the lowest occupancy carries the real configuration and
-- every occupancy above it inherits - beds by default, minutes whenever the
-- field is left empty. Only a category that genuinely differs per occupancy has
-- to say so, by switching on the override for that row.

CREATE TABLE IF NOT EXISTS functions.cost_bed_types (
    bed_type_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    enterprise_id text NOT NULL
        REFERENCES functions.cost_property_settings(enterprise_id)
        ON DELETE CASCADE,
    bed_name text NOT NULL,
    linen_cost numeric(18, 4) NOT NULL DEFAULT 0,
    sort_order integer NOT NULL DEFAULT 0,
    UNIQUE (enterprise_id, bed_name),
    CHECK (nullif(trim(bed_name), '') IS NOT NULL),
    CHECK (linen_cost >= 0)
);

-- Which beds are made up in one (category, occupancy) row.
--
-- The bed is referenced by NAME, not by bed_type_id. Saving the rulebook
-- rewrites every table in it, so an identity-column foreign key would be
-- dangling the moment the bed types were reinserted; the name survives the
-- rewrite, and the editor keeps the references in step when a bed is renamed.
CREATE TABLE IF NOT EXISTS functions.cost_cleaning_beds (
    cleaning_bed_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cleaning_category_id bigint NOT NULL
        REFERENCES functions.cost_cleaning_categories(cleaning_category_id)
        ON DELETE CASCADE,
    bed_name text NOT NULL,
    quantity integer NOT NULL DEFAULT 1,
    UNIQUE (cleaning_category_id, bed_name),
    CHECK (quantity >= 1),
    CHECK (nullif(trim(bed_name), '') IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_cost_bed_types_enterprise
    ON functions.cost_bed_types(enterprise_id, sort_order);
CREATE INDEX IF NOT EXISTS ix_cost_cleaning_beds_category
    ON functions.cost_cleaning_beds(cleaning_category_id);

-- An occupancy that carries its own bed setup rather than inheriting the
-- lowest occupancy's. Defaults to false, which is the common case and is also
-- correct for every existing row: none of them has beds to inherit yet.
ALTER TABLE functions.cost_cleaning_categories
    ADD COLUMN IF NOT EXISTS overrides_base boolean NOT NULL DEFAULT false;

-- "Not filled in" and "zero minutes" were the same value, so an occupancy that
-- should simply take the lowest occupancy's figure could not say so. Existing
-- rows all hold a real number and keep it - nothing starts inheriting by
-- surprise.
ALTER TABLE functions.cost_cleaning_categories
    ALTER COLUMN cleaning_minutes DROP NOT NULL;

-- The existing CHECK still holds: NULL >= 0 is NULL, which is not false, so a
-- blank row passes. Re-stated by name in case an older database carries the
-- constraint under the base schema's inline name.
ALTER TABLE functions.cost_cleaning_categories
    DROP CONSTRAINT IF EXISTS cost_cleaning_categories_cleaning_minutes_check;
ALTER TABLE functions.cost_cleaning_categories
    ADD CONSTRAINT cost_cleaning_categories_cleaning_minutes_check
    CHECK (cleaning_minutes IS NULL OR cleaning_minutes >= 0);

-- linen_cost is deliberately NOT dropped and NOT zeroed. It keeps whatever was
-- typed before bed types existed, and the application uses it for any row that
-- has no beds assigned yet, so a property migrates one category at a time
-- instead of losing its linen costs the moment this ships.

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('015_bed_types_and_cleaning_inheritance')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
