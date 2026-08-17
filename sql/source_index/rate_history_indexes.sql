/*
 * Source index for the rate-name lookup.
 *
 * Run against integration_db as its owner/DBA, outside a transaction.
 * The application reader must remain read-only and must not own these indexes.
 * CONCURRENTLY avoids blocking writes while each index is built.
 *
 * Why this one exists
 * -------------------
 * A rate's display name is read from rate_history rather than rate_current,
 * because the current row moves - a rate is renamed, or carries no name for a
 * while - and everything downstream is keyed on the name: the Cost Input
 * pickers store it and the distribution mix matches on it.
 *
 * The read is a LATERAL that takes the newest named version of one rate:
 *
 *     SELECT nullif(trim(history.name), '')
 *     FROM rate_history history
 *     WHERE history.id = <rate id>
 *       AND nullif(trim(history.name), '') IS NOT NULL
 *     ORDER BY history.created_utc DESC
 *     LIMIT 1
 *
 * It runs once per candidate rate - once per distinct rate on the matching-rate
 * picker, and once per reservation row feeding the distribution mix - so without
 * an index on (id, created_utc DESC) each one degrades to a scan of the whole
 * history, and the cost is multiplied by however many rates are in scope.
 *
 * The partial predicate matches the query's own: versions with no name are
 * never the answer, so they do not need to be in the index. Keep the two in
 * step - a query that stops filtering on name would stop being able to use a
 * partial index built around it. The column names are resolved at runtime from
 * shared/mews_source.py (RATE_HISTORY_NAME_COLUMNS, RATE_HISTORY_ORDER_COLUMNS);
 * if this mirror spells them differently, spell them the same way here.
 */

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rate_history_latest_named
    ON rate_history (id, created_utc DESC)
    INCLUDE (name)
    WHERE nullif(btrim(name), '') IS NOT NULL;
