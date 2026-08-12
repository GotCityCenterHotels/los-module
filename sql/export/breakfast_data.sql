WITH breakfast AS (
    SELECT DISTINCT
        service_id,
        accounting_category_id
    FROM product_current
    WHERE tenant_key = 'GCCH'
      AND name = 'Breakfast'
      AND is_active = true
)

SELECT
    md5(
        concat_ws(
            '|',
            ec.id::text,
            ((oi.start_utc AT TIME ZONE 'Europe/Stockholm')::date)::text
        )
    ) AS breakfast_data_key,

    ec.id AS enterprise_id,
    ec.name AS hotel_name,
    (oi.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS stay_date,

    count(oi.amount_net_value) AS breakfast_total,
    sum(oi.amount_net_value) AS breakfast_net_cost

FROM order_item_current oi

JOIN breakfast b
    ON b.service_id = oi.service_id
   AND b.accounting_category_id = oi.accounting_category_id

JOIN service_current sc
    ON sc.tenant_key = oi.tenant_key
   AND sc.id = oi.service_id

JOIN enterprise_current ec
    ON ec.tenant_key = sc.tenant_key
   AND ec.id = sc.enterprise_id

WHERE oi.tenant_key = 'GCCH'
  AND oi.canceled_utc IS NULL
  AND oi.start_utc IS NOT NULL

GROUP BY
    ec.id,
    ec.name,
    (oi.start_utc AT TIME ZONE 'Europe/Stockholm')::date

ORDER BY
    hotel_name,
    stay_date