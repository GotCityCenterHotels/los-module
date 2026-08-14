BEGIN;

CREATE SCHEMA IF NOT EXISTS functions;

CREATE TABLE IF NOT EXISTS functions.los_sync_runs (
    run_id bigserial PRIMARY KEY,
    mode text NOT NULL CHECK (mode IN ('delta', 'full')),
    status text NOT NULL CHECK (status IN ('running', 'published', 'failed')),
    source_watermark_from timestamptz,
    source_watermark_to timestamptz NOT NULL,
    source_as_of_date date NOT NULL,
    affected_reservations bigint NOT NULL DEFAULT 0,
    fact_rows bigint NOT NULL DEFAULT 0,
    aggregate_rows bigint NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    published_at timestamptz,
    error_message text
);

CREATE TABLE IF NOT EXISTS functions.reservation_los_fact (
    fact_key text PRIMARY KEY,
    fact_kind text NOT NULL CHECK (fact_kind IN ('current', 'historical')),
    reservation_number text NOT NULL,
    enterprise_id text NOT NULL
        REFERENCES functions.hotels(enterprise_id),
    arrival_date date NOT NULL,
    created_date date,
    cancelled_date date,
    los integer NOT NULL CHECK (los > 0),
    source_updated_at timestamptz NOT NULL,
    last_updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (fact_kind = 'current' AND created_date IS NULL AND cancelled_date IS NULL)
        OR fact_kind = 'historical'
    )
);

CREATE TABLE IF NOT EXISTS functions.los_reservation_identity (
    reservation_id uuid PRIMARY KEY,
    reservation_number text NOT NULL,
    source_updated_at timestamptz NOT NULL,
    last_updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_los_reservation_identity_number
    ON functions.los_reservation_identity (reservation_number);

CREATE INDEX IF NOT EXISTS ix_reservation_los_fact_number
    ON functions.reservation_los_fact (reservation_number);
CREATE INDEX IF NOT EXISTS ix_reservation_los_fact_reporting
    ON functions.reservation_los_fact (
        fact_kind, arrival_date, enterprise_id, los
    );

CREATE TABLE IF NOT EXISTS functions.reservation_los_daily (
    run_id bigint NOT NULL REFERENCES functions.los_sync_runs(run_id)
        ON DELETE CASCADE,
    comparison_basis text NOT NULL
        CHECK (comparison_basis IN ('sameDate', 'sameWeekday')),
    arrival_date date NOT NULL,
    enterprise_id text NOT NULL
        REFERENCES functions.hotels(enterprise_id),
    scenario text NOT NULL CHECK (scenario IN ('current', 'ly', 'spit')),
    los integer NOT NULL CHECK (los > 0),
    booking_count bigint NOT NULL CHECK (booking_count > 0),
    night_count bigint NOT NULL CHECK (night_count > 0),
    PRIMARY KEY (
        run_id, comparison_basis, arrival_date,
        enterprise_id, scenario, los
    ),
    CHECK (night_count = los::bigint * booking_count)
);

CREATE INDEX IF NOT EXISTS ix_reservation_los_daily_lookup
    ON functions.reservation_los_daily (
        run_id, comparison_basis, arrival_date, enterprise_id
    ) INCLUDE (scenario, los, booking_count, night_count);

CREATE TABLE IF NOT EXISTS functions.los_publication (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    run_id bigint NOT NULL REFERENCES functions.los_sync_runs(run_id),
    published_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS functions.schema_migrations (
    migration_name text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('008_los_read_model')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
