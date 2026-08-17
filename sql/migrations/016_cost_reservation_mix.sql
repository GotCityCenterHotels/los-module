BEGIN;

-- The two reservation-level mixes the GOP statement needs.
--
-- Until these existed, the Cost Data page had nothing but hotel-per-day totals,
-- so it blended every configured cleaning row into one mean per departure and
-- could apply nothing but the fallback distribution percentage. Both limits were
-- reported as flags on the statement, and neither could be fixed from the page.
--
-- Both tables hold a MIX, not a level. The authoritative totals stay where they
-- have always been - functions.arr_dep_data for departures and
-- functions.room_revenue_night_data for room revenue - and these two say how to
-- apportion them across the dimensions the rulebook is written in terms of. A
-- mix that is slightly out therefore cannot move the statement's totals, only
-- how they are split.

CREATE TABLE IF NOT EXISTS functions.departure_mix_data (
    departure_mix_data_key text PRIMARY KEY,

    enterprise_id text NOT NULL
        CONSTRAINT departure_mix_data_hotel_fkey
        REFERENCES functions.hotels(enterprise_id),
    hotel_name text NOT NULL,
    -- The departure date, so it buckets by the same column as every other cost
    -- fact and the page's Group by needs no special case for it.
    stay_date date NOT NULL,

    resource_category_id text,
    category_name text NOT NULL,
    -- Guests in the room on the night before departure. Cleaning is configured
    -- per category AND per occupancy, because linen and minutes differ by both.
    occupancy integer NOT NULL,
    departures integer NOT NULL,

    first_inserted_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_departure_mix_data_stay_date
ON functions.departure_mix_data (stay_date);

CREATE INDEX IF NOT EXISTS ix_departure_mix_data_enterprise_stay_date
ON functions.departure_mix_data (enterprise_id, stay_date);

-- last_seen_at is what the importer prunes on: a (category, occupancy) that no
-- longer has departures on a day has no row to overwrite, so without this index
-- a stale combination would keep its old count for good.
CREATE INDEX IF NOT EXISTS ix_departure_mix_data_last_seen
ON functions.departure_mix_data (last_seen_at);

CREATE TABLE IF NOT EXISTS functions.distribution_mix_data (
    distribution_mix_data_key text PRIMARY KEY,

    enterprise_id text NOT NULL
        CONSTRAINT distribution_mix_data_hotel_fkey
        REFERENCES functions.hotels(enterprise_id),
    hotel_name text NOT NULL,
    stay_date date NOT NULL,

    -- The three dimensions the distribution rulebook groups on, in the order it
    -- nests them. All three are nullable: a mirror that carries no travel agency
    -- or no rate on the reservation still supports the levels above it.
    origin text,
    travel_agency text,
    rate_name text,

    room_revenue_net numeric(18, 2) NOT NULL DEFAULT 0,
    reservation_count integer NOT NULL DEFAULT 0,

    first_inserted_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_distribution_mix_data_stay_date
ON functions.distribution_mix_data (stay_date);

CREATE INDEX IF NOT EXISTS ix_distribution_mix_data_enterprise_stay_date
ON functions.distribution_mix_data (enterprise_id, stay_date);

CREATE INDEX IF NOT EXISTS ix_distribution_mix_data_last_seen
ON functions.distribution_mix_data (last_seen_at);

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('016_cost_reservation_mix')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
