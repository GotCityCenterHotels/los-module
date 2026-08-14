-- Probe the integration_db source schema for the Cost Input pickers. READ ONLY.
--
-- Run against integration_db (Database B) and paste the output back. It answers
-- the three things the rate/channel picker and the cleaning-category generator
-- need, and which are not derivable from this repository.

-- 1. Exact columns of rate_current. Looking for: the rate's display name, how it
--    links to a hotel (enterprise_id directly, or only service_id), and the
--    active/public flags.
SELECT table_schema, column_name, data_type
FROM information_schema.columns
WHERE table_name = 'rate_current'
ORDER BY table_schema, ordinal_position;

-- 2. Capacity columns on resource_category_current. Mews exposes Capacity and
--    ExtraCapacity; cleaning occupancy steps run 1 .. (capacity + extra_capacity).
SELECT table_schema, column_name, data_type
FROM information_schema.columns
WHERE table_name = 'resource_category_current'
ORDER BY table_schema, ordinal_position;

-- 3. Anything that looks like a distribution channel. Mews has no channel table,
--    so this is usually an origin / channel-manager / business-segment column on
--    the reservation or a booking source dimension.
SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE column_name ~* 'channel|origin|source|segment|booker|travel_agent'
ORDER BY table_schema, table_name, column_name;

-- 4. Does a schema named "operations" exist, and what is in it?
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema = 'operations'
ORDER BY table_name;

-- 5. Sample of live rate names for one hotel, to confirm the shape of the list
--    the picker will show. Replace the enterprise id before running.
-- SELECT r.*
-- FROM rate_current r
-- LIMIT 20;
