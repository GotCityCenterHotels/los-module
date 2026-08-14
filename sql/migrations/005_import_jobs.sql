BEGIN;

CREATE SCHEMA IF NOT EXISTS functions;

CREATE TABLE IF NOT EXISTS functions.import_jobs (
    job_id uuid PRIMARY KEY,
    job_type text NOT NULL CHECK (job_type IN ('cost', 'supplement')),
    operation text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL CHECK (
        status IN ('queued', 'running', 'retrying', 'succeeded', 'failed')
    ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    result jsonb,
    error_message text
);

CREATE INDEX IF NOT EXISTS ix_import_jobs_status_created
    ON functions.import_jobs (status, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS ux_import_jobs_active_type
    ON functions.import_jobs (job_type)
    WHERE status IN ('queued', 'running', 'retrying');

CREATE TABLE IF NOT EXISTS functions.schema_migrations (
    migration_name text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('005_import_jobs')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
