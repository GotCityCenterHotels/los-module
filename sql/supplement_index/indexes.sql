CREATE INDEX CONCURRENTLY ix_resource_history_supplement_asof
ON public.resource_history (
    tenant_key,
    id,
    snapshot_valid_from DESC,
    snapshot_observed_at DESC,
    snapshot_id DESC
)
INCLUDE (state, is_active);

CREATE INDEX CONCURRENTLY ix_resource_category_assignment_history_asof
ON public.resource_category_assignment_history (
    tenant_key,
    id,
    snapshot_valid_from DESC,
    snapshot_observed_at DESC,
    snapshot_id DESC
)
INCLUDE (resource_id, category_id, is_active);

CREATE INDEX CONCURRENTLY ix_resource_category_history_asof
ON public.resource_category_history (
    tenant_key,
    id,
    snapshot_valid_from DESC,
    snapshot_observed_at DESC,
    snapshot_id DESC
)
INCLUDE (enterprise_id, service_id, type, is_active, space_name);