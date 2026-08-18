BEGIN;

CREATE SCHEMA IF NOT EXISTS functions;

-- One cheap, indexed row names the complete Cost Data publication. Fact
-- imports and Cost Input saves advance it after their writes commit, allowing
-- HTTP workers to reject stale response-cache entries without scanning every
-- fact and settings table for timestamps.
CREATE TABLE IF NOT EXISTS functions.cost_publication (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version bigint NOT NULL CHECK (version > 0),
    changed_at timestamptz NOT NULL DEFAULT now(),
    reason text NOT NULL
);

INSERT INTO functions.cost_publication (
    singleton, version, changed_at, reason
)
VALUES (true, 1, now(), 'migration')
ON CONFLICT (singleton) DO NOTHING;

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('017_cost_publication')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
