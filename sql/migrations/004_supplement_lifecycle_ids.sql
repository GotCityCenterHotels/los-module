-- Upgrade databases that already applied the original name-keyed migration 003.
-- The old Supplement facts came from the superseded source-relation contract,
-- so they cannot be mapped reliably to integration_db UUIDs. Rebuild only the
-- disposable Supplement read model; synchronization audit rows are preserved.
-- This migration runs once, after 003, on both upgraded and fresh databases.
DELETE FROM functions.supplement_publication;
DELETE FROM functions.supplement_coverage;

        DROP TABLE IF EXISTS functions.supplement_latest_detail CASCADE;
        DROP TABLE IF EXISTS functions.supplement_latest_category CASCADE;
        DROP TABLE IF EXISTS functions.supplement_latest_inventory CASCADE;
        DROP TABLE IF EXISTS functions.supplement_snapshot_detail CASCADE;
        DROP TABLE IF EXISTS functions.supplement_snapshot_category CASCADE;
        DROP TABLE IF EXISTS functions.supplement_snapshot_inventory CASCADE;
        DROP TABLE IF EXISTS functions.supplement_room_categories CASCADE;
        DROP TABLE IF EXISTS functions.supplement_hotels CASCADE;

        CREATE TABLE functions.supplement_hotels (
            hotel_code text PRIMARY KEY,
            tenant_key text NOT NULL,
            enterprise_id uuid NOT NULL,
            hotel_name text NOT NULL,
            active boolean NOT NULL DEFAULT true,
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_key, enterprise_id),
            CHECK (hotel_code = enterprise_id::text),
            CHECK (nullif(trim(hotel_name), '') IS NOT NULL)
        );

        CREATE TABLE functions.supplement_room_categories (
            hotel_code text NOT NULL
                REFERENCES functions.supplement_hotels(hotel_code),
            room_category_id uuid NOT NULL,
            space_room_name text NOT NULL,
            short_name text NOT NULL,
            sort_order integer NOT NULL DEFAULT 0,
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (hotel_code, room_category_id)
        );

        CREATE TABLE functions.supplement_snapshot_detail (
            snapshot_date date NOT NULL,
            stay_date date NOT NULL,
            hotel_code text NOT NULL,
            space_room_category_id uuid NOT NULL,
            space_room_name text NOT NULL,
            requested_room_category_id uuid NOT NULL,
            requested_room_name text NOT NULL,
            assigned_rooms numeric NOT NULL,
            room_revenue numeric NOT NULL,
            currency text NOT NULL DEFAULT 'SEK' CHECK (currency = 'SEK'),
            run_id bigint NOT NULL
                REFERENCES functions.supplement_sync_runs(run_id),
            PRIMARY KEY (
                snapshot_date, stay_date, hotel_code,
                space_room_category_id, requested_room_category_id
            ),
            CHECK (assigned_rooms >= 0),
            CHECK (room_revenue >= 0)
        ) PARTITION BY RANGE (snapshot_date);

        CREATE TABLE functions.supplement_snapshot_category (
            snapshot_date date NOT NULL,
            stay_date date NOT NULL,
            hotel_code text NOT NULL,
            space_room_category_id uuid NOT NULL,
            space_room_name text NOT NULL,
            assigned_rooms numeric NOT NULL,
            room_revenue numeric NOT NULL,
            currency text NOT NULL DEFAULT 'SEK' CHECK (currency = 'SEK'),
            run_id bigint NOT NULL
                REFERENCES functions.supplement_sync_runs(run_id),
            PRIMARY KEY (
                snapshot_date, stay_date, hotel_code, space_room_category_id
            ),
            CHECK (assigned_rooms >= 0),
            CHECK (room_revenue >= 0)
        ) PARTITION BY RANGE (snapshot_date);

        CREATE TABLE functions.supplement_snapshot_inventory (
            snapshot_date date NOT NULL,
            stay_date date NOT NULL,
            hotel_code text NOT NULL,
            space_room_category_id uuid NOT NULL,
            space_room_name text NOT NULL,
            total_space numeric NOT NULL,
            space_to_sell numeric NOT NULL,
            inventory_quality text NOT NULL CHECK (
                inventory_quality IN ('exact', 'approximated-current')
            ),
            run_id bigint NOT NULL
                REFERENCES functions.supplement_sync_runs(run_id),
            PRIMARY KEY (
                snapshot_date, stay_date, hotel_code, space_room_category_id
            ),
            CHECK (total_space >= 0),
            CHECK (space_to_sell >= 0),
            CHECK (space_to_sell <= total_space)
        ) PARTITION BY RANGE (snapshot_date);

        CREATE TABLE functions.supplement_latest_detail (
            stay_date date NOT NULL,
            hotel_code text NOT NULL,
            space_room_category_id uuid NOT NULL,
            space_room_name text NOT NULL,
            requested_room_category_id uuid NOT NULL,
            requested_room_name text NOT NULL,
            snapshot_date date NOT NULL,
            assigned_rooms numeric NOT NULL,
            room_revenue numeric NOT NULL,
            currency text NOT NULL DEFAULT 'SEK' CHECK (currency = 'SEK'),
            run_id bigint NOT NULL
                REFERENCES functions.supplement_sync_runs(run_id),
            PRIMARY KEY (
                stay_date, hotel_code,
                space_room_category_id, requested_room_category_id
            )
        );

        CREATE TABLE functions.supplement_latest_category (
            stay_date date NOT NULL,
            hotel_code text NOT NULL,
            space_room_category_id uuid NOT NULL,
            space_room_name text NOT NULL,
            snapshot_date date NOT NULL,
            assigned_rooms numeric NOT NULL,
            room_revenue numeric NOT NULL,
            currency text NOT NULL DEFAULT 'SEK' CHECK (currency = 'SEK'),
            run_id bigint NOT NULL
                REFERENCES functions.supplement_sync_runs(run_id),
            PRIMARY KEY (stay_date, hotel_code, space_room_category_id)
        );

        CREATE TABLE functions.supplement_latest_inventory (
            stay_date date NOT NULL,
            hotel_code text NOT NULL,
            space_room_category_id uuid NOT NULL,
            space_room_name text NOT NULL,
            snapshot_date date NOT NULL,
            total_space numeric NOT NULL,
            space_to_sell numeric NOT NULL,
            inventory_quality text NOT NULL CHECK (
                inventory_quality IN ('exact', 'approximated-current')
            ),
            run_id bigint NOT NULL
                REFERENCES functions.supplement_sync_runs(run_id),
            PRIMARY KEY (stay_date, hotel_code, space_room_category_id)
        );

        CREATE INDEX ix_supplement_snapshot_detail_lookup
        ON functions.supplement_snapshot_detail
            (hotel_code, stay_date, space_room_category_id, snapshot_date DESC);
        CREATE INDEX ix_supplement_snapshot_category_lookup
        ON functions.supplement_snapshot_category
            (hotel_code, stay_date, snapshot_date DESC);
        CREATE INDEX ix_supplement_snapshot_inventory_lookup
        ON functions.supplement_snapshot_inventory
            (hotel_code, stay_date, snapshot_date DESC);
        CREATE INDEX ix_supplement_latest_category_range
        ON functions.supplement_latest_category (hotel_code, stay_date);
        CREATE INDEX ix_supplement_latest_inventory_range
        ON functions.supplement_latest_inventory (hotel_code, stay_date);
INSERT INTO functions.schema_migrations (migration_name)
VALUES ('004_supplement_lifecycle_ids')
ON CONFLICT (migration_name) DO NOTHING;
