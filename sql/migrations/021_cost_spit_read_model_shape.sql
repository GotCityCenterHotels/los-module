BEGIN;

-- Migration 020 stored fact_rows as jsonb carrying the source query's
-- snake_case column names. That made every HTTP read pay for the shape twice:
-- psycopg parsed each array back into Python objects, the service rebuilt every
-- row to rename its keys, and json.dumps then re-encoded the lot. Measured on a
-- full calendar year - which is what the Cost Data page asks for by default,
-- 1 January to today - that was about 4.4 seconds of pure Python per uncached
-- request, which is the entire cost this read model exists to remove.
--
-- The rows are now stored in exactly the shape and key case the response sends,
-- and as json rather than jsonb. json keeps the text verbatim, so publishing is
-- a byte copy in and reading is a byte copy out: no parse, no per-row rebuild,
-- and no re-encode on the way to the browser. fact_count carries the row count
-- so rowCounts can still be reported without looking inside the array.
--
-- Dropping is safe and deliberate rather than a migration of the old rows:
-- every row here is derived from Database B and is rebuilt in full by the next
-- Cost Data import.
DROP TABLE IF EXISTS functions.cost_spit_publication;
DROP TABLE IF EXISTS functions.cost_spit_daily;
DROP TABLE IF EXISTS functions.cost_spit_sync_runs;

CREATE TABLE functions.cost_spit_sync_runs (
    run_id bigserial PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('running', 'published', 'failed')),
    source_as_of_date date NOT NULL,
    current_range_start date NOT NULL,
    current_range_end date NOT NULL,
    source_rows bigint NOT NULL DEFAULT 0 CHECK (source_rows >= 0),
    daily_rows bigint NOT NULL DEFAULT 0 CHECK (daily_rows >= 0),
    fact_rows bigint NOT NULL DEFAULT 0 CHECK (fact_rows >= 0),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    published_at timestamptz,
    error_message text,
    CHECK (current_range_start <= current_range_end)
);

-- Abandoned runs are pruned by started_at, so that lookup gets an index of its
-- own rather than sequentially scanning a table whose rows are cheap but whose
-- cascade targets are not.
CREATE INDEX cost_spit_sync_runs_status_started_idx
    ON functions.cost_spit_sync_runs (status, started_at);

-- One JSON array per dataset/day keeps the read model generic enough for the
-- seven established Cost Data row shapes while making a full-year lookup a few
-- thousand indexed rows. The source payload remains immutable per run;
-- publication pointers decide which complete run readers can see.
CREATE TABLE functions.cost_spit_daily (
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
    fact_count integer NOT NULL CHECK (fact_count >= 0),
    -- Response-ready: camelCase keys, nulls already stripped, and json rather
    -- than jsonb so neither publication nor the read reserialises it.
    fact_rows json NOT NULL CHECK (json_typeof(fact_rows) = 'array'),
    PRIMARY KEY (run_id, comparison_basis, stay_date, dataset)
);

CREATE TABLE functions.cost_spit_publication (
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
VALUES ('021_cost_spit_read_model_shape')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
