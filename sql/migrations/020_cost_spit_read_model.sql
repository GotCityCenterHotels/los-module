BEGIN;

CREATE SCHEMA IF NOT EXISTS functions;

CREATE TABLE IF NOT EXISTS functions.cost_spit_sync_runs (
    run_id bigserial PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('running', 'published', 'failed')),
    source_as_of_date date NOT NULL,
    current_range_start date NOT NULL,
    current_range_end date NOT NULL,
    source_rows bigint NOT NULL DEFAULT 0 CHECK (source_rows >= 0),
    daily_rows bigint NOT NULL DEFAULT 0 CHECK (daily_rows >= 0),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    published_at timestamptz,
    error_message text,
    CHECK (current_range_start <= current_range_end)
);

-- One JSON array per dataset/day keeps the read model generic enough for the
-- seven established Cost Data row shapes while making a full YTD lookup only a
-- few thousand indexed rows. The source payload remains immutable per run;
-- publication pointers decide which complete run readers can see.
CREATE TABLE IF NOT EXISTS functions.cost_spit_daily (
    run_id bigint NOT NULL
        REFERENCES functions.cost_spit_sync_runs(run_id) ON DELETE CASCADE,
    comparison_basis text NOT NULL
        CHECK (comparison_basis IN ('sameDate', 'sameWeekday')),
    stay_date date NOT NULL,
    dataset text NOT NULL CHECK (dataset IN (
        'arrivalsDepartures',
        'breakfast',
        'parking',
        'roomRevenue',
        'payments',
        'cleaningAllocations',
        'distributionMix'
    )),
    fact_rows jsonb NOT NULL CHECK (jsonb_typeof(fact_rows) = 'array'),
    PRIMARY KEY (run_id, comparison_basis, stay_date, dataset)
);

CREATE TABLE IF NOT EXISTS functions.cost_spit_publication (
    comparison_basis text PRIMARY KEY
        CHECK (comparison_basis IN ('sameDate', 'sameWeekday')),
    run_id bigint NOT NULL
        REFERENCES functions.cost_spit_sync_runs(run_id),
    cutoff_date date NOT NULL,
    minimum_stay_date date NOT NULL,
    maximum_stay_date date NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    CHECK (minimum_stay_date <= maximum_stay_date)
);

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('020_cost_spit_read_model')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
