import logging
import os

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from threading import Lock
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from shared.db import (
    HTTP_EXPORT_STATEMENT_TIMEOUT_MS,
    export_conninfo,
    get_export_connection,
)
from shared.mews_source import (
    CATEGORY_ORDERING_COLUMNS,
    UNORDERED_CATEGORY_RANK,
    resolve_optional_column,
)


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


PICKUP_HISTORY_SQL = """
/* Rebuild the pickup curve for one stay date directly from reservation
 * lifecycle, rather than from stored daily snapshots.
 *
 * The snapshot tables only hold dates a sync actually materialised, so the
 * curve started wherever the pipeline started and was pruned at 366 days. The
 * lifecycle carries created/cancelled dates for every reservation, so the
 * position on ANY past day is derivable - back to the first booking ever made
 * for the stay date, with no window to configure and nothing to backfill.
 *
 * The eligibility predicates and the "one room per reservation per category per
 * night" rule are deliberately identical to _materialize_snapshot_facts in
 * services/supplement_sync_service.py, so a reconstructed point equals the
 * stored point for any date both cover.
 *
 * Read-only: integration_db is opened with default_transaction_read_only=on.
 */
WITH assigned_category AS (
    SELECT a.tenant_key, a.resource_id, c.id AS category_id
    FROM resource_category_assignment_current a
    JOIN resource_category_current c
      ON c.tenant_key = a.tenant_key AND c.id = a.category_id
    WHERE a.is_active AND c.type = 'Room'
),
items AS (
    SELECT
        rc.id AS reservation_id,
        coalesce(assigned.category_id, requested.id) AS space_category_id,
        rc.created_utc::date AS reservation_created_date,
        rc.cancelled_utc::date AS reservation_cancelled_date,
        oi.created_utc::date AS item_created_date,
        oi.canceled_utc::date AS item_cancelled_date,
        oi.amount_gross_value::numeric AS gross_revenue
    FROM order_item_current oi
    JOIN reservation_current rc
      ON rc.tenant_key = oi.tenant_key AND rc.id = oi.service_order_id
    JOIN service_current service
      ON service.tenant_key = rc.tenant_key AND service.id = rc.service_id
     AND service.name = 'Stay'
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
      AND ec.id::text = %(hotel_code)s
      AND oi.start_utc >= (
            %(stay_date)s::date::timestamp AT TIME ZONE 'Europe/Stockholm'
          )
      AND oi.start_utc < (
            (%(stay_date)s::date + 1)::timestamp AT TIME ZONE 'Europe/Stockholm'
          )
),
scoped AS (
    SELECT * FROM items
    WHERE %(category)s::uuid IS NULL
       OR space_category_id = %(category)s::uuid
),
bounds AS (
    /* The first day anything was on the books. No floor is applied here: the
     * caller slices the window it wants, so the query path has no ceiling that
     * could silently clip a large request. */
    SELECT least(
               min(reservation_created_date),
               min(item_created_date)
           ) AS first_booking_date
    FROM scoped
),
days AS (
    SELECT generate_series(
               (SELECT first_booking_date FROM bounds),
               least(%(stay_date)s::date + 7, %(as_of_date)s::date),
               interval '1 day'
           )::date AS snapshot_date
    WHERE (SELECT first_booking_date FROM bounds) IS NOT NULL
),
reservation_nights AS (
    SELECT d.snapshot_date, s.reservation_id, s.space_category_id,
           1::numeric AS assigned_rooms,
           sum(s.gross_revenue) AS room_revenue
    FROM days d
    JOIN scoped s
      ON s.reservation_created_date <= d.snapshot_date
     AND (s.reservation_cancelled_date IS NULL
          OR s.reservation_cancelled_date > d.snapshot_date)
     AND s.item_created_date <= d.snapshot_date
     AND (s.item_cancelled_date IS NULL
          OR s.item_cancelled_date > d.snapshot_date)
    GROUP BY d.snapshot_date, s.reservation_id, s.space_category_id
)
SELECT snapshot_date,
       sum(assigned_rooms)::numeric AS assigned_rooms,
       coalesce(sum(room_revenue), 0)::numeric AS room_revenue
FROM reservation_nights
GROUP BY snapshot_date
ORDER BY snapshot_date
"""


# Space categories are listed in the Mews ordering everywhere in the app, so
# that ordering has to survive the trip through the read model. {ordering} and
# {history_ordering} are filled in by _inventory_sql() from whichever mirrored
# column actually carries ResourceCategory.Ordering; a mirror without one gets
# the unordered rank and keeps the previous name ordering.
INVENTORY_SQL_TEMPLATE = """
WITH snapshot_dates AS (
    SELECT unnest(%(snapshot_dates)s::date[]) AS snapshot_date
),
current_inventory AS (
    SELECT r.tenant_key, c.enterprise_id, trim(e.name)::text AS hotel_name,
           c.id AS category_id, trim(c.space_name)::text AS category_name,
           {ordering} AS category_ordering,
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
    GROUP BY r.tenant_key, c.enterprise_id, e.name, c.id, c.space_name{ordering_group}
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
           selected.is_active, selected.space_name, selected.category_ordering
    FROM historical_dates d
    CROSS JOIN resource_category_current category_key
    CROSS JOIN LATERAL (
        SELECT h.tenant_key, h.id, h.enterprise_id, h.service_id,
               h.type, h.is_active, h.space_name,
               {history_ordering} AS category_ordering
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
           coalesce(c.category_ordering, {unordered_rank})::int AS category_ordering,
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
             e.name, c.id, c.space_name, c.category_ordering
),
approximated_inventory AS (
    SELECT d.snapshot_date, i.*, 'approximated-current'::text AS inventory_quality
    FROM snapshot_dates d CROSS JOIN current_inventory i
    WHERE d.snapshot_date < DATE '2026-02-27'
)
SELECT * FROM historical_inventory
UNION ALL
SELECT * FROM approximated_inventory
ORDER BY snapshot_date, hotel_name, category_ordering, category_name
"""


def _render_inventory_sql(current_column, history_column):
    """INVENTORY_SQL for a mirror that may or may not expose the ordering.

    Column names come from CATEGORY_ORDERING_COLUMNS, a fixed tuple of plain
    identifiers, so they are never user input.
    """
    return INVENTORY_SQL_TEMPLATE.format(
        ordering=(
            f'coalesce(c."{current_column}", {UNORDERED_CATEGORY_RANK})::int'
            if current_column else f"{UNORDERED_CATEGORY_RANK}::int"
        ),
        ordering_group=f', c."{current_column}"' if current_column else "",
        history_ordering=(
            f'h."{history_column}"' if history_column else "NULL::int"
        ),
        unordered_rank=UNORDERED_CATEGORY_RANK,
    )


# The safe default: no ordering column anywhere, which reproduces the previous
# name ordering. iter_inventory_batches() swaps in the resolved variant once it
# has a connection to probe with.
INVENTORY_SQL = _render_inventory_sql(None, None)

_resolved_inventory_sql = None
_inventory_sql_lock = Lock()


def inventory_sql(connection):
    """INVENTORY_SQL bound to whichever ordering columns this mirror has.

    Resolved once per process against information_schema, the same way the
    Cost Input source columns are resolved.
    """
    global _resolved_inventory_sql
    if _resolved_inventory_sql is not None:
        return _resolved_inventory_sql
    with _inventory_sql_lock:
        if _resolved_inventory_sql is not None:
            return _resolved_inventory_sql
        with connection.cursor() as cursor:
            current_column = resolve_optional_column(
                cursor, "resource_category_current", CATEGORY_ORDERING_COLUMNS
            )
            history_column = resolve_optional_column(
                cursor, "resource_category_history", CATEGORY_ORDERING_COLUMNS
            )
        if current_column is None:
            logging.info(
                "No Mews ordering column on resource_category_current (tried %s); "
                "Supplement space categories keep name ordering",
                list(CATEGORY_ORDERING_COLUMNS),
            )
        _resolved_inventory_sql = _render_inventory_sql(
            current_column, history_column
        )
        return _resolved_inventory_sql


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


def _assert_source_boundary(connection, expected_database):
    """The connection really is integration_db, and really is read-only."""
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT current_database() AS database_name, "
            "current_setting('transaction_read_only') AS read_only"
        )
        boundary = cursor.fetchone()
    if boundary["database_name"].lower() != expected_database.lower():
        raise RuntimeError("Supplement source connection opened the wrong database")
    if boundary["read_only"].lower() != "on":
        raise RuntimeError("Supplement source connection is not read-only")


@contextmanager
def _read_only_source_connection(statement_timeout_ms=None):
    expected_database = _require_integration_settings()
    with get_export_connection(statement_timeout_ms) as connection:
        _assert_source_boundary(connection, expected_database)
        yield connection


# The interactive read path keeps a small pool of its own. The sync jobs open a
# connection per run and hold it for minutes, which is right for them and wrong
# here: a dialog that opens a fresh connection pays a TLS handshake and a SCRAM
# exchange before it can ask anything, every single time. Bounded at two because
# that is how many pickup curves a detail request rebuilds at once.
#
# The boundary assertion moves to the pool's configure hook, so it still runs
# against every physical connection - once, when the connection is created,
# rather than once per query.
_pickup_pool = None
_pickup_pool_lock = Lock()


def _pickup_connection_pool():
    global _pickup_pool
    if _pickup_pool is not None:
        return _pickup_pool
    with _pickup_pool_lock:
        if _pickup_pool is None:
            expected_database = _require_integration_settings()
            _pickup_pool = ConnectionPool(
                conninfo=export_conninfo(HTTP_EXPORT_STATEMENT_TIMEOUT_MS),
                kwargs={"row_factory": dict_row},
                configure=lambda connection: _assert_source_boundary(
                    connection, expected_database
                ),
                min_size=int(os.environ.get("SUPPLEMENT_SOURCE_POOL_MIN_SIZE", "0")),
                max_size=int(os.environ.get("SUPPLEMENT_SOURCE_POOL_MAX_SIZE", "2")),
                timeout=float(os.environ.get("DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "10")),
                max_idle=float(os.environ.get("DB_POOL_MAX_IDLE_SECONDS", "300")),
                max_lifetime=float(os.environ.get("DB_POOL_MAX_LIFETIME_SECONDS", "1800")),
                check=ConnectionPool.check_connection,
                open=True,
            )
    return _pickup_pool


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


def fetch_pickup_history(hotel_code, stay_date, category, as_of_date):
    """Full pickup curve for one stay date, rebuilt from reservation lifecycle.

    Returns one row per day from the first booking through the stay date (plus a
    week of post-stay corrections, bounded by as_of_date). The caller slices the
    window it wants to display; nothing here caps how far back it reaches.

    Unlike the sync paths this one serves a browser, so it takes the tighter
    HTTP statement ceiling: past ~45s Static Web Apps has already given up on
    the response, and anything still running is holding a source backend for a
    request nobody is waiting on any more. It also uses an ordinary cursor
    rather than a named one - the result is a row per day, so the DECLARE and
    FETCH round trips a server-side cursor adds buy nothing - and a pooled
    connection, so a warm instance does not re-handshake per dialog.
    """
    with _pickup_connection_pool().connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(PICKUP_HISTORY_SQL, {
                "hotel_code": str(hotel_code),
                "stay_date": stay_date,
                "category": category,
                "as_of_date": as_of_date,
            })
            return cursor.fetchall()


def iter_inventory_batches(snapshot_dates_value, batch_size=5000):
    with _read_only_source_connection() as connection:
        query = inventory_sql(connection)
        with connection.cursor(name="supplement_inventory") as cursor:
            cursor.execute(query, {"snapshot_dates": snapshot_dates_value})
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
    with _read_only_source_connection() as connection:
        query = inventory_sql(connection)
    return _explain(query, {"snapshot_dates": [snapshot_date]})
