BEGIN;

/*
 * Post-deployment cleanup. Apply only after every Function instance is using
 * functions.hotels (migration 006). This is deliberately not auto-registered.
 */
DO $migration$
DECLARE
    missing_count bigint;
BEGIN
    IF to_regclass('functions.hotels') IS NULL THEN
        RAISE EXCEPTION 'Cannot clean up legacy hotel tables: functions.hotels is missing';
    END IF;

    IF to_regclass('functions.cost_properties') IS NOT NULL THEN
        SELECT count(*) INTO missing_count
        FROM functions.cost_properties legacy
        WHERE NOT EXISTS (
            SELECT 1 FROM functions.hotels hotel
            WHERE hotel.enterprise_id = legacy.enterprise_id::text
        );
        IF missing_count > 0 THEN
            RAISE EXCEPTION
                'Cannot remove functions.cost_properties: % hotels were not migrated',
                missing_count;
        END IF;
    END IF;

    IF to_regclass('functions.supplement_hotels') IS NOT NULL THEN
        SELECT count(*) INTO missing_count
        FROM functions.supplement_hotels legacy
        WHERE NOT EXISTS (
            SELECT 1 FROM functions.hotels hotel
            WHERE hotel.enterprise_id = legacy.enterprise_id::text
        );
        IF missing_count > 0 THEN
            RAISE EXCEPTION
                'Cannot remove functions.supplement_hotels: % hotels were not migrated',
                missing_count;
        END IF;
    END IF;
END
$migration$;

ALTER TABLE IF EXISTS functions.cost_property_settings
    DROP COLUMN IF EXISTS hotel_name;

DROP INDEX IF EXISTS functions.ix_cost_property_settings_hotel_name;
DROP INDEX IF EXISTS functions.ix_cost_properties_hotel_name;
DROP INDEX IF EXISTS functions.ix_supplement_hotels_name;
DROP INDEX IF EXISTS functions.ix_arr_dep_data_hotel_name;
DROP INDEX IF EXISTS functions.ix_breakfast_data_hotel_name;
DROP INDEX IF EXISTS functions.ix_parking_data_hotel_name;
DROP INDEX IF EXISTS functions.ix_total_payment_data_hotel_name;

DROP TABLE IF EXISTS functions.supplement_hotels;
DROP TABLE IF EXISTS functions.cost_properties;

INSERT INTO functions.schema_migrations (migration_name)
VALUES ('007_remove_legacy_hotel_tables')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
