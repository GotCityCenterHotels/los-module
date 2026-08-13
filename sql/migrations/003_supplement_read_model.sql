CREATE SCHEMA IF NOT EXISTS functions;

CREATE TABLE IF NOT EXISTS functions.supplement_sync_runs (
    run_id bigserial PRIMARY KEY,
    mode text NOT NULL CHECK (mode IN ('delta', 'repair', 'backfill')),
    status text NOT NULL CHECK (status IN ('running', 'published', 'failed')),
    source_snapshot_from date,
    source_snapshot_to date,
    exported_rows bigint NOT NULL DEFAULT 0,
    imported_rows bigint NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    published_at timestamptz,
    error_message text
);

CREATE TABLE IF NOT EXISTS functions.supplement_publication (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    run_id bigint NOT NULL REFERENCES functions.supplement_sync_runs(run_id),
    data_as_of date NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS functions.supplement_coverage (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    minimum_stay_date date NOT NULL,
    maximum_stay_date date NOT NULL,
    minimum_snapshot_date date NOT NULL,
    maximum_snapshot_date date NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS functions.supplement_hotels (
    hotel_code text PRIMARY KEY,
    hotel_name text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    CHECK (nullif(trim(hotel_code), '') IS NOT NULL),
    CHECK (nullif(trim(hotel_name), '') IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS functions.supplement_room_categories (
    hotel_code text NOT NULL,
    space_room_name text NOT NULL,
    short_name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (hotel_code, space_room_name)
);

CREATE TABLE IF NOT EXISTS functions.supplement_snapshot_detail (
    snapshot_date date NOT NULL,
    stay_date date NOT NULL,
    hotel_code text NOT NULL,
    space_room_name text NOT NULL,
    requested_room_name text NOT NULL,
    assigned_rooms numeric NOT NULL,
    room_revenue numeric NOT NULL,
    currency text NOT NULL DEFAULT 'SEK',
    run_id bigint NOT NULL REFERENCES functions.supplement_sync_runs(run_id),
    PRIMARY KEY (
        snapshot_date, stay_date, hotel_code,
        space_room_name, requested_room_name
    ),
    CHECK (assigned_rooms >= 0)
) PARTITION BY RANGE (snapshot_date);

CREATE TABLE IF NOT EXISTS functions.supplement_snapshot_category (
    snapshot_date date NOT NULL,
    stay_date date NOT NULL,
    hotel_code text NOT NULL,
    space_room_name text NOT NULL,
    assigned_rooms numeric NOT NULL,
    room_revenue numeric NOT NULL,
    currency text NOT NULL DEFAULT 'SEK',
    run_id bigint NOT NULL REFERENCES functions.supplement_sync_runs(run_id),
    PRIMARY KEY (snapshot_date, stay_date, hotel_code, space_room_name),
    CHECK (assigned_rooms >= 0)
) PARTITION BY RANGE (snapshot_date);

CREATE TABLE IF NOT EXISTS functions.supplement_snapshot_inventory (
    snapshot_date date NOT NULL,
    stay_date date NOT NULL,
    hotel_code text NOT NULL,
    space_room_name text NOT NULL,
    total_space numeric NOT NULL,
    space_to_sell numeric NOT NULL,
    run_id bigint NOT NULL REFERENCES functions.supplement_sync_runs(run_id),
    PRIMARY KEY (snapshot_date, stay_date, hotel_code, space_room_name),
    CHECK (total_space >= 0),
    CHECK (space_to_sell >= 0)
) PARTITION BY RANGE (snapshot_date);

CREATE TABLE IF NOT EXISTS functions.supplement_latest_detail (
    stay_date date NOT NULL,
    hotel_code text NOT NULL,
    space_room_name text NOT NULL,
    requested_room_name text NOT NULL,
    snapshot_date date NOT NULL,
    assigned_rooms numeric NOT NULL,
    room_revenue numeric NOT NULL,
    currency text NOT NULL DEFAULT 'SEK',
    run_id bigint NOT NULL REFERENCES functions.supplement_sync_runs(run_id),
    PRIMARY KEY (stay_date, hotel_code, space_room_name, requested_room_name)
);

CREATE TABLE IF NOT EXISTS functions.supplement_latest_category (
    stay_date date NOT NULL,
    hotel_code text NOT NULL,
    space_room_name text NOT NULL,
    snapshot_date date NOT NULL,
    assigned_rooms numeric NOT NULL,
    room_revenue numeric NOT NULL,
    currency text NOT NULL DEFAULT 'SEK',
    run_id bigint NOT NULL REFERENCES functions.supplement_sync_runs(run_id),
    PRIMARY KEY (stay_date, hotel_code, space_room_name)
);

CREATE TABLE IF NOT EXISTS functions.supplement_latest_inventory (
    stay_date date NOT NULL,
    hotel_code text NOT NULL,
    space_room_name text NOT NULL,
    snapshot_date date NOT NULL,
    total_space numeric NOT NULL,
    space_to_sell numeric NOT NULL,
    run_id bigint NOT NULL REFERENCES functions.supplement_sync_runs(run_id),
    PRIMARY KEY (stay_date, hotel_code, space_room_name)
);

CREATE INDEX IF NOT EXISTS ix_supplement_snapshot_detail_lookup
ON functions.supplement_snapshot_detail
    (hotel_code, stay_date, space_room_name, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS ix_supplement_snapshot_category_lookup
ON functions.supplement_snapshot_category
    (hotel_code, stay_date, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS ix_supplement_snapshot_inventory_lookup
ON functions.supplement_snapshot_inventory
    (hotel_code, stay_date, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS ix_supplement_latest_category_range
ON functions.supplement_latest_category (hotel_code, stay_date);

CREATE INDEX IF NOT EXISTS ix_supplement_latest_inventory_range
ON functions.supplement_latest_inventory (hotel_code, stay_date);

CREATE OR REPLACE FUNCTION functions.ensure_supplement_month_partitions(target_date date)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    month_start date := date_trunc('month', target_date)::date;
    month_end date := (month_start + interval '1 month')::date;
    suffix text := to_char(month_start, 'YYYYMM');
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS functions.%I PARTITION OF functions.supplement_snapshot_detail FOR VALUES FROM (%L) TO (%L)',
        'supplement_snapshot_detail_' || suffix, month_start, month_end
    );
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS functions.%I PARTITION OF functions.supplement_snapshot_category FOR VALUES FROM (%L) TO (%L)',
        'supplement_snapshot_category_' || suffix, month_start, month_end
    );
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS functions.%I PARTITION OF functions.supplement_snapshot_inventory FOR VALUES FROM (%L) TO (%L)',
        'supplement_snapshot_inventory_' || suffix, month_start, month_end
    );
END;
$$;

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('003_supplement_read_model')
ON CONFLICT (migration_name) DO NOTHING;
