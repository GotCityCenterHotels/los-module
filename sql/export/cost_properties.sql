SELECT
    ec.id::text AS enterprise_id,
    ec.tenant_key::text AS tenant_key,
    trim(ec.name)::text AS hotel_name
FROM enterprise_current AS ec
WHERE ec.tenant_key = 'GCCH'
  AND ec.id IS NOT NULL
  AND ec.name IS NOT NULL
  AND trim(ec.name) <> ''
ORDER BY hotel_name, enterprise_id;
