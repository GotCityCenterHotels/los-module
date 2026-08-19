CREATE INDEX CONCURRENTLY cost_spit_item_window
ON order_item_current (tenant_key, start_utc)
INCLUDE (service_order_id, service_id, type, enterprise_id,
         accounting_category_id, amount_currency,
         amount_net_value, amount_gross_value,
         created_utc, canceled_utc);