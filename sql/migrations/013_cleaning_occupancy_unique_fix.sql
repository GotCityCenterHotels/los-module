BEGIN;

-- Migration 012 re-keyed cleaning rows on (enterprise_id, category_name,
-- occupancy) but failed to remove the old uniqueness, so saving a category at
-- more than one occupancy raised a unique violation and the whole PUT failed
-- with "Unable to save cost settings".
--
-- The reason 012 missed it: the old uniqueness exists in two different forms
-- depending on how the database was built.
--   * migration 001 created a standalone UNIQUE INDEX
--     (ux_cost_cleaning_categories_enterprise_name)
--   * the base schema created a table-level UNIQUE constraint
--     (cost_cleaning_categories_enterprise_id_category_name_key)
-- 012 only issued ALTER TABLE ... DROP CONSTRAINT, which does not drop a
-- standalone index. Both forms are removed here by name.

DROP INDEX IF EXISTS functions.ux_cost_cleaning_categories_enterprise_name;

ALTER TABLE functions.cost_cleaning_categories
    DROP CONSTRAINT IF EXISTS cost_cleaning_categories_enterprise_id_category_name_key;

-- An earlier version of this migration swept pg_index for any other unique
-- index over those two columns. It failed on "operator does not exist:
-- name[] = text[]" (pg_attribute.attname is name, not text), which aborted the
-- whole transaction and prevented the two drops above from applying at all.
-- The dynamic sweep was defensive cleverness guarding against a naming this
-- codebase never produces; the two explicit drops cover both forms that exist.
-- The diagnostic at the end reports anything unexpected that survived.

-- The intended key. 012 already creates this; repeated for databases that
-- somehow applied 012 partially.
CREATE UNIQUE INDEX IF NOT EXISTS ux_cost_cleaning_categories_occupancy
ON functions.cost_cleaning_categories (enterprise_id, category_name, occupancy);

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('013_cleaning_occupancy_unique_fix')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;

-- Diagnostic, outside the transaction. Every unique index left on the table.
-- The only one expected is ux_cost_cleaning_categories_occupancy over
-- (enterprise_id, category_name, occupancy). Anything else keyed on
-- category_name without occupancy will still reject multi-occupancy saves.
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'functions'
  AND tablename = 'cost_cleaning_categories'
ORDER BY indexname;
