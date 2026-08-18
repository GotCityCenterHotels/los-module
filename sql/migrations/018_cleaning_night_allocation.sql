BEGIN;

-- Cleaning is configured once per reservation, but the Cost Data chart is a
-- stay-night chart.  Keep the old departure count during the rolling upgrade
-- and add the allocation explicitly: a three-night stay writes 1/3 to each
-- occupied date.  The browser sums these shares against the category/occupancy
-- cleaning rate, so the reservation's total cost is unchanged while no longer
-- being charged entirely on checkout day.
ALTER TABLE functions.departure_mix_data
    ADD COLUMN IF NOT EXISTS allocated_cleanings numeric(18, 8);

ALTER TABLE functions.departure_mix_data
    ALTER COLUMN departures DROP NOT NULL;

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('018_cleaning_night_allocation')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
