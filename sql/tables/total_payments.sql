CREATE TABLE IF NOT EXISTS functions.total_payment_data (
    total_payment_data_key text PRIMARY KEY,

    enterprise_id text NOT NULL
        CONSTRAINT total_payment_data_hotel_fkey
        REFERENCES functions.hotels(enterprise_id),
    hotel_name text,
    stay_date date NOT NULL,
    amount_currency text NOT NULL,

    total_payment_amount_gross_value numeric(18, 2),

    first_inserted_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_total_payment_data_enterprise_id
ON functions.total_payment_data (enterprise_id);

CREATE INDEX IF NOT EXISTS ix_total_payment_data_hotel_name
ON functions.total_payment_data (hotel_name);

CREATE INDEX IF NOT EXISTS ix_total_payment_data_stay_date
ON functions.total_payment_data (stay_date);

CREATE INDEX IF NOT EXISTS ix_total_payment_data_amount_currency
ON functions.total_payment_data (amount_currency);
