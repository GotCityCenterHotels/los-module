/*
 * Run against integration_db as its owner/DBA, outside a transaction.
 * The application reader must remain read-only and must not own these indexes.
 * CONCURRENTLY avoids blocking writes while each index is built.
 */

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resource_history_supplement_asof
    ON resource_history (
        tenant_key,
        id,
        snapshot_valid_from DESC,
        snapshot_observed_at DESC,
        snapshot_id DESC
    )
    INCLUDE (state, is_active);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resource_category_assignment_history_asof
    ON resource_category_assignment_history (
        tenant_key,
        id,
        snapshot_valid_from DESC,
        snapshot_observed_at DESC,
        snapshot_id DESC
    )
    INCLUDE (resource_id, category_id, is_active);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_resource_category_history_asof
    ON resource_category_history (
        tenant_key,
        id,
        snapshot_valid_from DESC,
        snapshot_observed_at DESC,
        snapshot_id DESC
    )
    INCLUDE (enterprise_id, service_id, type, is_active, space_name);

ANALYZE resource_history;
ANALYZE resource_category_assignment_history;
ANALYZE resource_category_history;
