INSERT INTO functions.total_payment_data (
    total_payment_data_key,
    enterprise_id,
    hotel_name,
    stay_date,
    amount_currency,
    total_payment_amount_gross_value,
    first_inserted_at,
    last_seen_at,
    last_updated_at
)
VALUES (
    %(total_payment_data_key)s,
    %(enterprise_id)s,
    %(hotel_name)s,
    %(stay_date)s,
    %(amount_currency)s,
    %(total_payment_amount_gross_value)s,
    now(),
    now(),
    now()
)
ON CONFLICT (total_payment_data_key) DO UPDATE SET
    enterprise_id = EXCLUDED.enterprise_id,
    hotel_name = EXCLUDED.hotel_name,
    stay_date = EXCLUDED.stay_date,
    amount_currency = EXCLUDED.amount_currency,
    total_payment_amount_gross_value = EXCLUDED.total_payment_amount_gross_value,
    last_seen_at = now(),
    last_updated_at =
        CASE
            WHEN (
                functions.total_payment_data.enterprise_id,
                functions.total_payment_data.hotel_name,
                functions.total_payment_data.stay_date,
                functions.total_payment_data.amount_currency,
                functions.total_payment_data.total_payment_amount_gross_value
            ) IS DISTINCT FROM (
                EXCLUDED.enterprise_id,
                EXCLUDED.hotel_name,
                EXCLUDED.stay_date,
                EXCLUDED.amount_currency,
                EXCLUDED.total_payment_amount_gross_value
            )
            THEN now()
            ELSE functions.total_payment_data.last_updated_at
        END