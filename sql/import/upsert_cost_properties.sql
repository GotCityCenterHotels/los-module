INSERT INTO functions.cost_properties (
    enterprise_id,
    tenant_key,
    hotel_name,
    first_inserted_at,
    last_seen_at,
    last_updated_at
)
VALUES (
    %(enterprise_id)s,
    %(tenant_key)s,
    %(hotel_name)s,
    now(),
    now(),
    now()
)
ON CONFLICT (enterprise_id) DO UPDATE SET
    tenant_key = EXCLUDED.tenant_key,
    hotel_name = EXCLUDED.hotel_name,
    last_seen_at = now(),
    last_updated_at = CASE
        WHEN (
            functions.cost_properties.tenant_key,
            functions.cost_properties.hotel_name
        ) IS DISTINCT FROM (
            EXCLUDED.tenant_key,
            EXCLUDED.hotel_name
        )
        THEN now()
        ELSE functions.cost_properties.last_updated_at
    END;
