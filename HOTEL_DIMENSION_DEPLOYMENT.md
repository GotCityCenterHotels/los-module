# Unified hotel dimension deployment

`functions.hotels` is the canonical hotel dimension for Cost and Supplement data.
LOS remains read-only in `integration_db`, so its queries resolve the same
`enterprise_current.id` and expose it as `enterpriseId`; they cannot directly
join a table in Database A without moving the LOS facts across databases.

## Rollout

1. Deploy the Function code. Migration `006_unified_hotels.sql` runs
   automatically on the first Cost or Supplement schema initialization.
2. Run the `properties` cost import, then a Supplement delta sync. Both writers
   now upsert `functions.hotels`.
3. Verify that Cost, LOS, and Supplement endpoints return all expected hotels
   and that no import job is running on an old Function instance.
4. During a quiet window, apply
   `sql/migrations/007_remove_legacy_hotel_tables.sql` to Database A.

Migration 006 is intentionally additive: it retains the old hotel tables and
makes the duplicated Cost settings name nullable so old and new Function
instances can overlap during an Azure rolling deployment. Migration 007 is
deliberately not in either automatic migration list. It validates that every
legacy ID exists in `functions.hotels` before dropping:

- `functions.cost_properties`
- `functions.supplement_hotels`
- `functions.cost_property_settings.hotel_name`
- obsolete hotel-name indexes

Both migrations are transactional. A validation failure aborts the migration
without partially removing the legacy schema.
