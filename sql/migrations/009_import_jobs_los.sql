BEGIN;

ALTER TABLE functions.import_jobs
    DROP CONSTRAINT IF EXISTS import_jobs_job_type_check;
ALTER TABLE functions.import_jobs
    ADD CONSTRAINT import_jobs_job_type_check
    CHECK (job_type IN ('cost', 'supplement', 'los'));

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('009_import_jobs_los')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
