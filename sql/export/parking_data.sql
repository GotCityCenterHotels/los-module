WITH parking_capacity AS (
    SELECT
        rc.tenant_key,
        rcc.service_id,
        COUNT(DISTINCT rc.id) AS total_parking_spots
    FROM resource_current AS rc
    JOIN resource_category_assignment_current AS rcac
        ON rcac.tenant_key = rc.tenant_key
       AND rcac.resource_id = rc.id
       AND rcac.is_active = true
    JOIN resource_category_current AS rcc
        ON rcc.tenant_key = rcac.tenant_key
       AND rcc.id = rcac.category_id
    JOIN service_current AS sc
        ON sc.tenant_key = rcc.tenant_key
       AND sc.id = rcc.service_id
    WHERE sc.name = 'Parkering'
      AND rc.is_active = true
    GROUP BY
        rc.tenant_key,
        rcc.service_id
)

SELECT
    md5(
        concat_ws(
            '|',
            ec.id::text,
            sc.name,
            ((oi.start_utc AT TIME ZONE 'Europe/Stockholm')::date)::text
        )
    ) AS parking_data_key,

    ec.id AS enterprise_id,
    ec.name AS hotel_name,
    sc.name AS service,
    (oi.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS stay_date,

    COUNT(DISTINCT oi.service_order_id) AS total_reservations_using_parking,
    COALESCE(pc.total_parking_spots, 0) AS total_parking_spots,
    SUM(oi.amount_net_value) AS total_parking_amount_net_value

FROM service_current AS sc
JOIN enterprise_current AS ec
    ON ec.tenant_key = sc.tenant_key
   AND ec.id = sc.enterprise_id
JOIN order_item_current AS oi
    ON oi.tenant_key = sc.tenant_key
   AND oi.service_id = sc.id
   AND oi.canceled_utc IS NULL
   AND oi.start_utc IS NOT NULL
LEFT JOIN parking_capacity AS pc
    ON pc.tenant_key = sc.tenant_key
   AND pc.service_id = sc.id
WHERE sc.name = 'Parkering'
GROUP BY
    ec.id,
    ec.name,
    sc.name,
    (oi.start_utc AT TIME ZONE 'Europe/Stockholm')::date,
    pc.total_parking_spots
ORDER BY
    ec.id,
    ec.name,
    stay_date;