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

-- ---------------------------------------------------------------------------
-- The distribution tree (origin -> travel agency -> rate) needs three more
-- answers. Query 3's regex above does NOT cover them: "travel_agent" does not
-- match "travel_agency", and nothing there looks for a rate on the reservation.
--
-- Until these are answered, services/cost_source_service.py resolves each
-- column against a candidate list and reports what it could not honour in the
-- "capabilities" block of /api/costdata/sources. The editor stays usable
-- either way - it falls back to typed values - but the pickers stay empty.
-- ---------------------------------------------------------------------------

-- 6. Every column on reservation_current, verbatim. Nothing else settles which
--    of origin / travel_agency_id / rate_id the ETL actually flattened, or
--    whether the ids are uuid or text.
SELECT table_schema, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'reservation_current'
ORDER BY table_schema, ordinal_position;

-- 7. Agency, company and rate linkage anywhere in the mirror.
SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE column_name ~* 'agency|agent|company|corporate|account|booker|iata|rate'
ORDER BY table_schema, table_name, column_name;

-- 8. The travel agency table. This one is ANSWERED: it is staging.travel_agency.
--    Note the schema - it is outside the search path, so every reference to it
--    has to be qualified, and an unqualified information_schema probe will not
--    find it at all.
--
--    What is still resolved at runtime is its shape, because the application
--    supports two and picks whichever the mirror actually has:
--      * a dimension keyed by its own id, with reservation_current holding the
--        foreign key (how Mews models it: Reservation.TravelAgencyId); or
--      * a reservation-scoped landing table, one row per reservation, joined
--        on reservation_id / reservation_number.
--    Run this to see which, and confirm the column holding the name.
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'staging' AND table_name = 'travel_agency'
ORDER BY ordinal_position;

-- 8b. Anything else agency-shaped, in case a second copy exists elsewhere.
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_name ~* '^(company|companies|travel_agency|agency|account|customer)(_current|_history)?$'
ORDER BY table_schema, table_name;

-- 8c. Is the read-only role actually granted the staging schema? Without this
--     the table resolves to "not found" and the agency filter degrades to free
--     text with nothing obviously wrong.
SELECT has_schema_privilege(current_user, 'staging', 'USAGE') AS can_use_schema,
       has_table_privilege(current_user, 'staging.travel_agency', 'SELECT') AS can_select;

-- 9. Distinct origin values and their cardinality, so the picker can be
--    checked against reality (enum string vs numeric code). Replace the
--    column if query 6 says otherwise.
-- SELECT origin, count(*)
-- FROM reservation_current
-- WHERE start_utc >= now() - interval '2 years'
-- GROUP BY 1 ORDER BY 2 DESC;

-- 10. Indexes on the driving tables. This decides whether the origin, agency
--     and matching-rate lookups are index scans or repeated scans of
--     reservation_current. The lookups are bounded to a two-year window
--     (COST_SOURCE_WINDOW_DAYS), but the window only helps if start_utc is
--     indexed alongside the service key.
SELECT schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE tablename IN (
    'reservation_current', 'service_current', 'rate_current', 'company_current'
)
ORDER BY tablename, indexname;

-- 11. Is pg_trgm available? The agency search is ILIKE '%term%', which cannot
--     use a btree index. It is only worth a trigram index if the company table
--     turns out to be large.
SELECT name, installed_version
FROM pg_available_extensions
WHERE name = 'pg_trgm';
