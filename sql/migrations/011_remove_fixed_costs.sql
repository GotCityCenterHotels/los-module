BEGIN;

-- Fixed costs are not part of the per-property cost algorithm. They are applied
-- once at the analysis stage from a separately maintained roadmap, so keeping
-- them in the property rulebook only invited them to be double counted.
--
-- DESTRUCTIVE: this drops any fixed cost lines previously entered on the Cost
-- Input page. Export functions.cost_fixed_lines first if that data is still
-- wanted for the analysis spreadsheet.

DROP TABLE IF EXISTS functions.cost_fixed_lines;

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('011_remove_fixed_costs')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
