import os

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from shared.db import get_export_connection


INVENTORY_EXACT_FROM = date(2026, 2, 27)
STOCKHOLM = ZoneInfo("Europe/Stockholm")


BOOKING_LIFECYCLE_SQL = """
WITH assigned_category AS (
    SELECT a.tenant_key, a.resource_id, c.id AS category_id,
           c.space_name AS category_name
    FROM resource_category_assignment_current a
    JOIN resource_category_current c
      ON c.tenant_key = a.tenant_key AND c.id = a.category_id
    WHERE a.is_active AND c.type = 'Room'
)
SELECT
    oi.tenant_key,
    rc.id AS reservation_id,
    oi.id AS order_item_id,
    (oi.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS stay_date,
    rc.created_utc::date AS reservation_created_date,
    rc.cancelled_utc::date AS reservation_cancelled_date,
    oi.created_utc::date AS item_created_date,
    oi.canceled_utc::date AS item_cancelled_date,
    ec.id AS enterprise_id,
    trim(ec.name)::text AS hotel_name,
    requested.id AS requested_category_id,
    trim(requested.space_name)::text AS requested_category_name,
    coalesce(assigned.category_id, requested.id) AS space_category_id,
    trim(coalesce(assigned.category_name, requested.space_name))::text
        AS space_category_name,
    oi.amount_currency,
    oi.amount_gross_value::numeric AS gross_revenue
FROM order_item_current oi
JOIN reservation_current rc
  ON rc.tenant_key = oi.tenant_key AND rc.id = oi.service_order_id
JOIN service_current service
  ON service.tenant_key = rc.tenant_key AND service.id = rc.service_id
JOIN enterprise_current ec
  ON ec.tenant_key = service.tenant_key AND ec.id = service.enterprise_id
LEFT JOIN resource_category_current requested
  ON requested.tenant_key = rc.tenant_key
 AND requested.id = rc.requested_resource_category_id
 AND requested.type = 'Room'
LEFT JOIN assigned_category assigned
  ON assigned.tenant_key = rc.tenant_key
 AND assigned.resource_id = rc.assigned_resource_id
WHERE oi.type = 'SpaceOrder'
  AND service.name = 'Stay'
  AND oi.start_utc >= (
        %(minimum_stay_date)s::date::timestamp AT TIME ZONE 'Europe/Stockholm'
      )
  AND oi.start_utc < (
        (%(maximum_stay_date)s::date + 1)::timestamp AT TIME ZONE 'Europe/Stockholm'
      )
  AND rc.created_utc::date <= %(maximum_snapshot_date)s
  AND (rc.cancelled_utc IS NULL OR rc.cancelled_utc::date > %(minimum_snapshot_date)s)
  AND oi.created_utc::date <= %(maximum_snapshot_date)s
  AND (oi.canceled_utc IS NULL OR oi.canceled_utc::date > %(minimum_snapshot_date)s)
"""


INVENTORY_SQL = """
WITH snapshot_dates AS (
    SELECT unnest(%(snapshot_dates)s::date[]) AS snapshot_date
),
current_inventory AS (
    SELECT r.tenant_key, c.enterprise_id, trim(e.name)::text AS hotel_name,
           c.id AS category_id, trim(c.space_name)::text AS category_name,
           count(DISTINCT r.id)::numeric AS physical_inventory,
           count(DISTINCT r.id) FILTER (WHERE r.state <> 'OutOfOrder')::numeric
               AS sellable_inventory
    FROM resource_current r
    JOIN resource_category_assignment_current a
      ON a.tenant_key = r.tenant_key AND a.resource_id = r.id AND a.is_active
    JOIN resource_category_current c
      ON c.tenant_key = a.tenant_key AND c.id = a.category_id
     AND c.type = 'Room' AND c.is_active
    JOIN service_current service
      ON service.tenant_key = c.tenant_key AND service.id = c.service_id
     AND service.name = 'Stay'
    JOIN enterprise_current e
      ON e.tenant_key = c.tenant_key AND e.id = c.enterprise_id
    WHERE r.is_active
    GROUP BY r.tenant_key, c.enterprise_id, e.name, c.id, c.space_name
),
historical_dates AS (
    SELECT snapshot_date,
           ((snapshot_date + 1)::timestamp AT TIME ZONE 'Europe/Stockholm') AS cutoff
    FROM snapshot_dates WHERE snapshot_date >= DATE '2026-02-27'
),
resource_asof AS (
    SELECT d.snapshot_date, selected.tenant_key, selected.id,
           selected.state, selected.is_active
    FROM historical_dates d
    CROSS JOIN resource_current resource_key
    CROSS JOIN LATERAL (
        SELECT h.tenant_key, h.id, h.state, h.is_active
        FROM resource_history h
        WHERE h.tenant_key = resource_key.tenant_key
          AND h.id = resource_key.id
          AND h.snapshot_valid_from < d.cutoff
        ORDER BY h.snapshot_valid_from DESC,
                 h.snapshot_observed_at DESC, h.snapshot_id DESC
        LIMIT 1
    ) selected
),
assignment_asof AS (
    SELECT d.snapshot_date, selected.tenant_key, selected.id,
           selected.resource_id, selected.category_id, selected.is_active
    FROM historical_dates d
    CROSS JOIN resource_category_assignment_current assignment_key
    CROSS JOIN LATERAL (
        SELECT h.tenant_key, h.id, h.resource_id, h.category_id, h.is_active
        FROM resource_category_assignment_history h
        WHERE h.tenant_key = assignment_key.tenant_key
          AND h.id = assignment_key.id
          AND h.snapshot_valid_from < d.cutoff
        ORDER BY h.snapshot_valid_from DESC,
                 h.snapshot_observed_at DESC, h.snapshot_id DESC
        LIMIT 1
    ) selected
),
category_asof AS (
    SELECT d.snapshot_date, selected.tenant_key, selected.id,
           selected.enterprise_id, selected.service_id, selected.type,
           selected.is_active, selected.space_name
    FROM historical_dates d
    CROSS JOIN resource_category_current category_key
    CROSS JOIN LATERAL (
        SELECT h.tenant_key, h.id, h.enterprise_id, h.service_id,
               h.type, h.is_active, h.space_name
        FROM resource_category_history h
        WHERE h.tenant_key = category_key.tenant_key
          AND h.id = category_key.id
          AND h.snapshot_valid_from < d.cutoff
        ORDER BY h.snapshot_valid_from DESC,
                 h.snapshot_observed_at DESC, h.snapshot_id DESC
        LIMIT 1
    ) selected
),
historical_inventory AS (
    SELECT r.snapshot_date, r.tenant_key, c.enterprise_id,
           trim(e.name)::text AS hotel_name, c.id AS category_id,
           trim(c.space_name)::text AS category_name,
           count(DISTINCT r.id)::numeric AS physical_inventory,
           count(DISTINCT r.id) FILTER (WHERE r.state <> 'OutOfOrder')::numeric
               AS sellable_inventory,
           'exact'::text AS inventory_quality
    FROM resource_asof r
    JOIN assignment_asof a
      ON a.snapshot_date = r.snapshot_date AND a.tenant_key = r.tenant_key
     AND a.resource_id = r.id AND a.is_active
    JOIN category_asof c
      ON c.snapshot_date = a.snapshot_date AND c.tenant_key = a.tenant_key
     AND c.id = a.category_id AND c.type = 'Room' AND c.is_active
    JOIN service_current service
      ON service.tenant_key = c.tenant_key AND service.id = c.service_id
     AND service.name = 'Stay'
    JOIN enterprise_current e
      ON e.tenant_key = c.tenant_key AND e.id = c.enterprise_id
    WHERE r.is_active
    GROUP BY r.snapshot_date, r.tenant_key, c.enterprise_id,
             e.name, c.id, c.space_name
),
approximated_inventory AS (
    SELECT d.snapshot_date, i.*, 'approximated-current'::text AS inventory_quality
    FROM snapshot_dates d CROSS JOIN current_inventory i
    WHERE d.snapshot_date < DATE '2026-02-27'
)
SELECT * FROM historical_inventory
UNION ALL
SELECT * FROM approximated_inventory
ORDER BY snapshot_date, hotel_name, category_name
"""


def _require_integration_settings():
    required = (
        "INTEGRATION_DB_HOST",
        "INTEGRATION_DB_USER",
        "INTEGRATION_DB_PASSWORD",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Supplement synchronization requires dedicated Database B settings: "
            + ", ".join(missing)
        )
    database_name = os.environ.get("INTEGRATION_DB_NAME", "integration_db")
    if database_name.lower() != "integration_db":
        raise RuntimeError("Supplement Database B must be integration_db")
    return database_name


@contextmanager
def _read_only_source_connection():
    expected_database = _require_integration_settings()
    with get_export_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database() AS database_name, "
                "current_setting('transaction_read_only') AS read_only"
            )
            boundary = cursor.fetchone()
        if boundary["database_name"].lower() != expected_database.lower():
            raise RuntimeError("Supplement source connection opened the wrong database")
        if boundary["read_only"].lower() != "on":
            raise RuntimeError("Supplement source connection is not read-only")
        yield connection


def stockholm_today():
    return datetime.now(STOCKHOLM).date()


def snapshot_dates(snapshot_from, snapshot_to):
    return [
        snapshot_from + timedelta(days=offset)
        for offset in range((snapshot_to - snapshot_from).days + 1)
    ]


def booking_parameters(snapshot_dates_value, minimum_stay_date, maximum_stay_date):
    return {
        "minimum_snapshot_date": min(snapshot_dates_value),
        "maximum_snapshot_date": max(snapshot_dates_value),
        "minimum_stay_date": minimum_stay_date,
        "maximum_stay_date": maximum_stay_date,
    }


def iter_booking_lifecycle_batches(
    snapshot_dates_value,
    minimum_stay_date,
    maximum_stay_date,
    batch_size=5000,
):
    parameters = booking_parameters(
        snapshot_dates_value, minimum_stay_date, maximum_stay_date
    )
    with _read_only_source_connection() as connection:
        with connection.cursor(name="supplement_booking_lifecycle") as cursor:
            cursor.execute(BOOKING_LIFECYCLE_SQL, parameters)
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    return
                yield rows


def iter_inventory_batches(snapshot_dates_value, batch_size=5000):
    with _read_only_source_connection() as connection:
        with connection.cursor(name="supplement_inventory") as cursor:
            cursor.execute(INVENTORY_SQL, {"snapshot_dates": snapshot_dates_value})
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    return
                yield rows


def _explain(query, parameters):
    with _read_only_source_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "EXPLAIN (ANALYZE, BUFFERS, SETTINGS, SUMMARY, FORMAT JSON) " + query,
                parameters,
            )
            return next(iter(cursor.fetchone().values()))


def explain_booking_lifecycle(snapshot_date, maximum_stay_date):
    return _explain(
        BOOKING_LIFECYCLE_SQL,
        booking_parameters(
            [snapshot_date], snapshot_date - timedelta(days=7), maximum_stay_date
        ),
    )


def explain_inventory(snapshot_date):
    return _explain(INVENTORY_SQL, {"snapshot_dates": [snapshot_date]})
