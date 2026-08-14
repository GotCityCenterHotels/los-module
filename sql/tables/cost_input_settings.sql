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

CREATE TABLE IF NOT EXISTS functions.cost_property_settings (
    enterprise_id text PRIMARY KEY
        CONSTRAINT cost_property_settings_hotel_fkey
        REFERENCES functions.hotels(enterprise_id),
    currency text NOT NULL DEFAULT 'SEK',
    distribution_default_percent numeric(7, 4) NOT NULL DEFAULT 0,
    cleaning_cost_per_minute numeric(18, 4) NOT NULL DEFAULT 0,
    reception_cost_per_hour numeric(18, 4) NOT NULL DEFAULT 0,
    room_rent_percent numeric(7, 4) NOT NULL DEFAULT 0,
    breakfast_calculation_basis text NOT NULL DEFAULT 'guests',
    breakfast_food_cost_per_guest numeric(18, 4) NOT NULL DEFAULT 0,
    breakfast_staff_cost_per_hour numeric(18, 4) NOT NULL DEFAULT 0,
    breakfast_rent_percent numeric(7, 4) NOT NULL DEFAULT 0,
    parking_rent_percent numeric(7, 4) NOT NULL DEFAULT 0,
    card_cost_percent numeric(7, 4) NOT NULL DEFAULT 2,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (currency ~ '^[A-Z]{3}$'),
    CHECK (breakfast_calculation_basis IN ('guests', 'products')),
    CHECK (distribution_default_percent BETWEEN 0 AND 100),
    CHECK (room_rent_percent BETWEEN 0 AND 100),
    CHECK (breakfast_rent_percent BETWEEN 0 AND 100),
    CHECK (parking_rent_percent BETWEEN 0 AND 100),
    CHECK (card_cost_percent BETWEEN 0 AND 100),
    CHECK (cleaning_cost_per_minute >= 0),
    CHECK (reception_cost_per_hour >= 0),
    CHECK (breakfast_food_cost_per_guest >= 0),
    CHECK (breakfast_staff_cost_per_hour >= 0)
);

CREATE TABLE IF NOT EXISTS functions.cost_distribution_groups (
    distribution_group_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    enterprise_id text NOT NULL REFERENCES functions.cost_property_settings(enterprise_id) ON DELETE CASCADE,
    group_name text NOT NULL,
    cost_percent numeric(7, 4) NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    UNIQUE (enterprise_id, group_name),
    CHECK (cost_percent BETWEEN 0 AND 100)
);

CREATE TABLE IF NOT EXISTS functions.cost_distribution_rules (
    distribution_rule_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    distribution_group_id bigint NOT NULL REFERENCES functions.cost_distribution_groups(distribution_group_id) ON DELETE CASCADE,
    match_type text NOT NULL CHECK (match_type IN ('rate', 'channel')),
    match_value text NOT NULL,
    UNIQUE (distribution_group_id, match_type, match_value)
);

CREATE TABLE IF NOT EXISTS functions.cost_cleaning_categories (
    cleaning_category_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    enterprise_id text NOT NULL REFERENCES functions.cost_property_settings(enterprise_id) ON DELETE CASCADE,
    category_name text NOT NULL,
    -- One row per (room category, occupancy): a category serving 2 + 1 extra
    -- beds has three rows, because linen and minutes differ per occupancy.
    -- Uniqueness therefore MUST include occupancy - keying on category_name
    -- alone rejects every row after the first.
    resource_category_id text,
    occupancy integer NOT NULL DEFAULT 1,
    min_guests integer NOT NULL DEFAULT 1,
    max_guests integer,
    cleaning_minutes numeric(10, 2) NOT NULL DEFAULT 0,
    linen_cost numeric(18, 4) NOT NULL DEFAULT 0,
    sort_order integer NOT NULL DEFAULT 0,
    UNIQUE (enterprise_id, category_name, occupancy),
    CHECK (occupancy >= 1),
    CHECK (min_guests >= 0),
    CHECK (max_guests IS NULL OR max_guests >= min_guests),
    CHECK (cleaning_minutes >= 0),
    CHECK (linen_cost >= 0)
);

CREATE TABLE IF NOT EXISTS functions.cost_arrival_staffing_tiers (
    arrival_tier_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    enterprise_id text NOT NULL REFERENCES functions.cost_property_settings(enterprise_id) ON DELETE CASCADE,
    min_arrivals integer NOT NULL,
    max_arrivals integer,
    reception_hours numeric(10, 2) NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    CHECK (min_arrivals >= 0),
    CHECK (max_arrivals IS NULL OR max_arrivals >= min_arrivals),
    CHECK (reception_hours >= 0)
);

CREATE TABLE IF NOT EXISTS functions.cost_breakfast_staffing_tiers (
    breakfast_tier_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    enterprise_id text NOT NULL REFERENCES functions.cost_property_settings(enterprise_id) ON DELETE CASCADE,
    min_guests integer NOT NULL,
    max_guests integer,
    staff_hours numeric(10, 2) NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    CHECK (min_guests >= 0),
    CHECK (max_guests IS NULL OR max_guests >= min_guests),
    CHECK (staff_hours >= 0)
);

-- Fixed costs are intentionally absent: they are applied once at the analysis
-- stage from a separately maintained roadmap, not per property in the cost
-- algorithm. Migration 011 drops the table on databases that still have it.

CREATE INDEX IF NOT EXISTS ix_hotels_tenant_active_name ON functions.hotels(tenant_key, active, hotel_name, enterprise_id);
CREATE INDEX IF NOT EXISTS ix_cost_distribution_groups_enterprise ON functions.cost_distribution_groups(enterprise_id);
CREATE INDEX IF NOT EXISTS ix_cost_cleaning_categories_enterprise ON functions.cost_cleaning_categories(enterprise_id);
CREATE INDEX IF NOT EXISTS ix_cost_arrival_tiers_enterprise ON functions.cost_arrival_staffing_tiers(enterprise_id);
CREATE INDEX IF NOT EXISTS ix_cost_breakfast_tiers_enterprise ON functions.cost_breakfast_staffing_tiers(enterprise_id);

CREATE TABLE IF NOT EXISTS functions.schema_migrations (
    migration_name text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('001_cost_settings_enterprise_text')
ON CONFLICT (migration_name) DO NOTHING;

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('002_cost_properties')
ON CONFLICT (migration_name) DO NOTHING;
