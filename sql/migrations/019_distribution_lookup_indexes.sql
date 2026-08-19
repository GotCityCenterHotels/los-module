BEGIN;

-- Index the rulebook lateral in the distributionRates query the way it is
-- actually probed.
--
-- queries/cost_data.py joins the two value tables on a folded comparison:
--
--     AND lower(btrim(group_origin.origin_value)) = lower(btrim(combination.origin))
--     AND lower(btrim(rate_value.rate_name))      = lower(btrim(combination.rate_name))
--
-- The existing indexes cover only the group foreign key, so PostgreSQL could seek
-- to a group's rows but then had to evaluate lower(btrim(...)) on every one of
-- them to find the match. Folding the expression into the index lets it seek
-- straight to the row instead. The lateral runs once per distinct
-- (enterprise, origin, agency, rate) combination - 989 on a production year - so
-- the saving is per combination, not per fact row.
--
-- The expressions have to match the query character for character or the planner
-- will not use these. If either predicate in cost_data.py is ever reworded, these
-- have to be reworded with it.
--
-- Nothing is added for cost_distribution_agency_filters. Its predicate is a
-- punctuation-stripped strpos() containment test, which no btree can satisfy;
-- indexing it would need a trigram index on a table of a few dozen rows, and the
-- write cost would outweigh a scan of that size.

CREATE INDEX IF NOT EXISTS ix_cost_distribution_origin_values_folded
    ON functions.cost_distribution_origin_values (
        origin_group_id, lower(btrim(origin_value))
    );

CREATE INDEX IF NOT EXISTS ix_cost_distribution_rate_values_folded
    ON functions.cost_distribution_rate_values (
        rate_group_id, lower(btrim(rate_name))
    );

-- Statistics for the folded expressions. Without these the planner estimates
-- selectivity on the raw columns, which is what made it prefer a sequential scan
-- of a group's values over the seek these indexes now allow.
ANALYZE functions.cost_distribution_origin_values;
ANALYZE functions.cost_distribution_rate_values;

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('019_distribution_lookup_indexes')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
