BEGIN;

-- Three related changes to the property cost rulebook.
--
-- 1. Arrival cost can be switched off. A property that does not staff reception
--    by arrival volume previously had to leave the thresholds empty, which the
--    Cost Data page then reported as a configuration gap on every load. An
--    explicit "off" is a decision; an empty list is an omission, and the two
--    must not look the same.
--
-- 2. Franchise gets its own percentage. It used to be folded into the three
--    rent percentages, which the Cost Data page had to flag as an approximation
--    on every single load because Cost Input had no franchise field at all.
--    The card cost percentage moves alongside it - it is a payment-processing
--    fee charged on the same statement, not rent.
--
-- 3. Distribution costs become a three-level tree: origin group -> travel
--    agency subgroup -> rate group. Each level carries its own percentage and
--    the deeper match wins, so "everything from this channel manager is 15%,
--    except this agency at 12%, except these two rates at 9%" is expressible
--    without enumerating every combination.
--
-- The flat functions.cost_distribution_groups / _rules tables are NOT dropped.
-- Destructive DDL is applied by hand in this codebase (see 007 and 011), and
-- the data is copied into the tree below rather than translated in place.

-- ---------------------------------------------------------------------------
-- 1 & 2. Profile columns
-- ---------------------------------------------------------------------------

ALTER TABLE functions.cost_property_settings
    ADD COLUMN IF NOT EXISTS arrival_cost_enabled boolean NOT NULL DEFAULT true;

ALTER TABLE functions.cost_property_settings
    ADD COLUMN IF NOT EXISTS franchise_enabled boolean NOT NULL DEFAULT false;

ALTER TABLE functions.cost_property_settings
    ADD COLUMN IF NOT EXISTS franchise_percent numeric(7, 4) NOT NULL DEFAULT 0;

-- 'net' uses the net revenue figures the cost facts carry directly. 'gross'
-- grosses them up by franchise_vat_percent, because every revenue column in
-- the fact tables is net of VAT and there is no gross revenue anywhere in the
-- source - only gross payments, which are not the same thing.
ALTER TABLE functions.cost_property_settings
    ADD COLUMN IF NOT EXISTS franchise_basis text NOT NULL DEFAULT 'net';

ALTER TABLE functions.cost_property_settings
    ADD COLUMN IF NOT EXISTS franchise_revenue_base text NOT NULL
        DEFAULT 'roomInclProducts';

-- Swedish lodging VAT. Only read when franchise_basis = 'gross'.
ALTER TABLE functions.cost_property_settings
    ADD COLUMN IF NOT EXISTS franchise_vat_percent numeric(7, 4) NOT NULL
        DEFAULT 12;

ALTER TABLE functions.cost_property_settings
    DROP CONSTRAINT IF EXISTS cost_property_settings_franchise_percent_check;
ALTER TABLE functions.cost_property_settings
    ADD CONSTRAINT cost_property_settings_franchise_percent_check
    CHECK (franchise_percent BETWEEN 0 AND 100);

ALTER TABLE functions.cost_property_settings
    DROP CONSTRAINT IF EXISTS cost_property_settings_franchise_vat_percent_check;
ALTER TABLE functions.cost_property_settings
    ADD CONSTRAINT cost_property_settings_franchise_vat_percent_check
    CHECK (franchise_vat_percent BETWEEN 0 AND 100);

ALTER TABLE functions.cost_property_settings
    DROP CONSTRAINT IF EXISTS cost_property_settings_franchise_basis_check;
ALTER TABLE functions.cost_property_settings
    ADD CONSTRAINT cost_property_settings_franchise_basis_check
    CHECK (franchise_basis IN ('net', 'gross'));

ALTER TABLE functions.cost_property_settings
    DROP CONSTRAINT IF EXISTS cost_property_settings_franchise_revenue_base_check;
ALTER TABLE functions.cost_property_settings
    ADD CONSTRAINT cost_property_settings_franchise_revenue_base_check
    CHECK (franchise_revenue_base IN (
        'roomInclProducts',
        'roomExclProducts',
        'roomExclProductsPlusParking',
        'totalRevenue'
    ));

-- ---------------------------------------------------------------------------
-- 3. Distribution tree
-- ---------------------------------------------------------------------------

-- Level 1: the origin group. Its fallback applies to every reservation whose
-- origin is listed here and which no subgroup claims.
CREATE TABLE IF NOT EXISTS functions.cost_distribution_origin_groups (
    origin_group_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    enterprise_id text NOT NULL
        REFERENCES functions.cost_property_settings(enterprise_id)
        ON DELETE CASCADE,
    group_name text NOT NULL,
    fallback_percent numeric(7, 4) NOT NULL DEFAULT 0,
    sort_order integer NOT NULL DEFAULT 0,
    UNIQUE (enterprise_id, group_name),
    CHECK (fallback_percent BETWEEN 0 AND 100)
);

-- The origins the group matches. Empty means the group matches nothing yet,
-- which is a half-finished group rather than a catch-all - a catch-all would
-- silently swallow every other group's reservations.
CREATE TABLE IF NOT EXISTS functions.cost_distribution_origin_values (
    origin_value_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    origin_group_id bigint NOT NULL
        REFERENCES functions.cost_distribution_origin_groups(origin_group_id)
        ON DELETE CASCADE,
    origin_value text NOT NULL,
    UNIQUE (origin_group_id, origin_value)
);

-- Level 2: the travel agency subgroup, narrowing its parent origin group.
CREATE TABLE IF NOT EXISTS functions.cost_distribution_agency_groups (
    agency_group_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    origin_group_id bigint NOT NULL
        REFERENCES functions.cost_distribution_origin_groups(origin_group_id)
        ON DELETE CASCADE,
    group_name text NOT NULL,
    fallback_percent numeric(7, 4) NOT NULL DEFAULT 0,
    sort_order integer NOT NULL DEFAULT 0,
    UNIQUE (origin_group_id, group_name),
    CHECK (fallback_percent BETWEEN 0 AND 100)
);

-- "travel agency contains X", matched without regard to case. match_field is
-- present so a second searchable attribute can be added later without another
-- table; today the editor only writes 'travelAgency'.
CREATE TABLE IF NOT EXISTS functions.cost_distribution_agency_filters (
    agency_filter_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agency_group_id bigint NOT NULL
        REFERENCES functions.cost_distribution_agency_groups(agency_group_id)
        ON DELETE CASCADE,
    match_field text NOT NULL DEFAULT 'travelAgency',
    contains_value text NOT NULL,
    UNIQUE (agency_group_id, match_field, contains_value),
    CHECK (match_field IN ('travelAgency', 'company', 'channel')),
    CHECK (nullif(trim(contains_value), '') IS NOT NULL)
);

-- Level 3: named rates, with the percentage that actually applies to them.
CREATE TABLE IF NOT EXISTS functions.cost_distribution_rate_groups (
    rate_group_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agency_group_id bigint NOT NULL
        REFERENCES functions.cost_distribution_agency_groups(agency_group_id)
        ON DELETE CASCADE,
    group_name text NOT NULL,
    cost_percent numeric(7, 4) NOT NULL DEFAULT 0,
    sort_order integer NOT NULL DEFAULT 0,
    UNIQUE (agency_group_id, group_name),
    CHECK (cost_percent BETWEEN 0 AND 100)
);

-- rate_id is the Mews id when the rate was picked from the source list and
-- NULL when it was typed by hand. The name is what the cost algorithm matches
-- on, because that is the only rate identifier the fact tables could ever
-- carry - the id is stored so a renamed rate can be re-resolved.
CREATE TABLE IF NOT EXISTS functions.cost_distribution_rate_values (
    rate_value_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rate_group_id bigint NOT NULL
        REFERENCES functions.cost_distribution_rate_groups(rate_group_id)
        ON DELETE CASCADE,
    rate_id text,
    rate_name text NOT NULL,
    UNIQUE (rate_group_id, rate_name),
    CHECK (nullif(trim(rate_name), '') IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_cost_distribution_origin_groups_enterprise
    ON functions.cost_distribution_origin_groups(enterprise_id, sort_order);
CREATE INDEX IF NOT EXISTS ix_cost_distribution_origin_values_group
    ON functions.cost_distribution_origin_values(origin_group_id);
CREATE INDEX IF NOT EXISTS ix_cost_distribution_agency_groups_parent
    ON functions.cost_distribution_agency_groups(origin_group_id, sort_order);
CREATE INDEX IF NOT EXISTS ix_cost_distribution_agency_filters_group
    ON functions.cost_distribution_agency_filters(agency_group_id);
CREATE INDEX IF NOT EXISTS ix_cost_distribution_rate_groups_parent
    ON functions.cost_distribution_rate_groups(agency_group_id, sort_order);
CREATE INDEX IF NOT EXISTS ix_cost_distribution_rate_values_group
    ON functions.cost_distribution_rate_values(rate_group_id);

-- ---------------------------------------------------------------------------
-- Carry the flat groups into the tree
--
-- A flat group held rate matches and channel matches side by side with one
-- percentage. In the tree those belong at different depths, so each flat group
-- becomes one origin group carrying its percentage as the fallback:
--   * its channel matches become that group's origins - channel values came
--     from the reservation origin column in the first place, so this is the
--     same data in the same place;
--   * its rate matches become a rate group at the same percentage, under a
--     pass-through "All travel agencies" subgroup.
-- Every configured percentage and every matched value survives, at the depth
-- where the tree can act on it. Runs once, and only for properties that have
-- no tree yet, so re-applying this migration cannot duplicate anything.
-- ---------------------------------------------------------------------------

WITH migratable AS (
    SELECT g.distribution_group_id, g.enterprise_id, g.group_name,
           g.cost_percent, g.sort_order
    FROM functions.cost_distribution_groups g
    WHERE NOT EXISTS (
        SELECT 1 FROM functions.cost_distribution_origin_groups existing
        WHERE existing.enterprise_id = g.enterprise_id
    )
),
inserted_origin AS (
    INSERT INTO functions.cost_distribution_origin_groups (
        enterprise_id, group_name, fallback_percent, sort_order
    )
    SELECT enterprise_id, group_name, cost_percent, sort_order
    FROM migratable
    ON CONFLICT (enterprise_id, group_name) DO NOTHING
    RETURNING origin_group_id, enterprise_id, group_name
),
paired AS (
    SELECT m.distribution_group_id, m.cost_percent, o.origin_group_id
    FROM migratable m
    JOIN inserted_origin o
      ON o.enterprise_id = m.enterprise_id
     AND o.group_name = m.group_name
),
copied_origins AS (
    INSERT INTO functions.cost_distribution_origin_values (
        origin_group_id, origin_value
    )
    SELECT DISTINCT p.origin_group_id, trim(r.match_value)
    FROM paired p
    JOIN functions.cost_distribution_rules r
      ON r.distribution_group_id = p.distribution_group_id
    WHERE r.match_type = 'channel'
      AND nullif(trim(r.match_value), '') IS NOT NULL
    ON CONFLICT DO NOTHING
    RETURNING 1
),
-- Only groups that actually had rate matches need the pass-through subgroup;
-- creating one for every group would litter the editor with empty levels.
groups_with_rates AS (
    SELECT DISTINCT p.origin_group_id, p.cost_percent
    FROM paired p
    JOIN functions.cost_distribution_rules r
      ON r.distribution_group_id = p.distribution_group_id
    WHERE r.match_type = 'rate'
      AND nullif(trim(r.match_value), '') IS NOT NULL
),
inserted_agency AS (
    INSERT INTO functions.cost_distribution_agency_groups (
        origin_group_id, group_name, fallback_percent, sort_order
    )
    SELECT origin_group_id, 'All travel agencies', cost_percent, 0
    FROM groups_with_rates
    ON CONFLICT (origin_group_id, group_name) DO NOTHING
    RETURNING agency_group_id, origin_group_id
),
inserted_rate_group AS (
    INSERT INTO functions.cost_distribution_rate_groups (
        agency_group_id, group_name, cost_percent, sort_order
    )
    SELECT a.agency_group_id, 'Carried over rates', g.cost_percent, 0
    FROM inserted_agency a
    JOIN groups_with_rates g USING (origin_group_id)
    ON CONFLICT (agency_group_id, group_name) DO NOTHING
    RETURNING rate_group_id, agency_group_id
)
INSERT INTO functions.cost_distribution_rate_values (rate_group_id, rate_name)
SELECT DISTINCT rg.rate_group_id, trim(r.match_value)
FROM inserted_rate_group rg
JOIN inserted_agency a USING (agency_group_id)
JOIN paired p USING (origin_group_id)
JOIN functions.cost_distribution_rules r
  ON r.distribution_group_id = p.distribution_group_id
WHERE r.match_type = 'rate'
  AND nullif(trim(r.match_value), '') IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('014_franchise_and_distribution_tree')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
