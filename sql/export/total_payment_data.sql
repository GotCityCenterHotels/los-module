SELECT
    md5(
        concat_ws(
            '|',
            ec.id::text,
            ((oi.start_utc AT TIME ZONE 'Europe/Stockholm')::date)::text,
            coalesce(oi.amount_currency, '')
        )
    ) AS total_payment_data_key,

    ec.id AS enterprise_id,
    ec.name AS hotel_name,
    (oi.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS stay_date,
    oi.amount_currency,
    sum(oi.amount_gross_value) AS total_payment_amount_gross_value

FROM order_item_current oi
JOIN enterprise_current ec
    ON ec.tenant_key = oi.tenant_key
   AND ec.id = oi.enterprise_id

WHERE oi.canceled_utc IS NULL
  AND oi.start_utc IS NOT NULL

GROUP BY
    ec.name,
    ec.id,
    (oi.start_utc AT TIME ZONE 'Europe/Stockholm')::date,
    oi.amount_currency

ORDER BY
    ec.name,
    ec.id,
    stay_date,
    oi.amount_currency;