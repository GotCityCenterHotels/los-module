"""Read-only lookups against integration_db for the Cost Input pickers.

integration_db is read-only at both the role and session level (see
shared/db.py), and everything here is a bounded SELECT.

The source mirrors the Mews Connector API, but the ETL flattens localized text
into single columns and the exact naming varies per table (resource categories
expose "space_name", not "names"). Rather than hard-coding a guess, each column
is resolved once from information_schema against a candidate list and cached for
the process. A missing column then produces an explicit message naming the
table and the candidates tried, instead of a bare UndefinedColumn at runtime.
"""

import logging

from threading import Lock

from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier

from shared.db import get_export_connection
from shared.mews_source import (
    CATEGORY_ORDERING_COLUMNS,
    UNORDERED_CATEGORY_RANK,
)


class CostSourceUnavailableError(RuntimeError):
    pass


_column_cache = {}
_column_lock = Lock()

# Ordered by likelihood. The first column present on the table wins.
RATE_NAME_COLUMNS = ("rate_name", "name", "names", "short_name", "display_name")
RATE_ACTIVE_COLUMNS = ("is_active", "active", "is_enabled")
RATE_ENTERPRISE_COLUMNS = ("enterprise_id",)
CATEGORY_NAME_COLUMNS = ("space_name", "category_name", "name", "names", "short_names")
CATEGORY_CAPACITY_COLUMNS = ("capacity", "normal_bed_count", "standard_occupancy")
CATEGORY_EXTRA_CAPACITY_COLUMNS = ("extra_capacity", "extra_bed_count", "extra_occupancy")
CHANNEL_COLUMNS = (
    "origin", "channel_manager_name", "channel_manager", "booking_source",
    "source", "channel",
)


def _table_columns(cursor, table_name):
    """Column names present on a source table, cached per process."""
    if table_name in _column_cache:
        return _column_cache[table_name]
    with _column_lock:
        if table_name in _column_cache:
            return _column_cache[table_name]
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s
            """,
            (table_name,),
        )
        columns = {row["column_name"] for row in cursor.fetchall()}
        _column_cache[table_name] = columns
        return columns


def _resolve_column(cursor, table_name, candidates, required=True):
    columns = _table_columns(cursor, table_name)
    if not columns:
        raise CostSourceUnavailableError(
            f"Source table '{table_name}' was not found in integration_db."
        )
    for candidate in candidates:
        if candidate in columns:
            return candidate
    if not required:
        return None
    raise CostSourceUnavailableError(
        f"None of {list(candidates)} exist on '{table_name}'. "
        f"Available columns: {sorted(columns)}"
    )


def _reset_column_cache():
    """Test seam - the cache is keyed by table name only."""
    with _column_lock:
        _column_cache.clear()


def list_rates(enterprise_id):
    """Active rates for one hotel.

    Mews Rate has no EnterpriseId - it hangs off ServiceId - so the join runs
    through service_current unless the mirror denormalised enterprise_id onto
    the rate itself.
    """
    with get_export_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            name_column = _resolve_column(cursor, "rate_current", RATE_NAME_COLUMNS)
            active_column = _resolve_column(
                cursor, "rate_current", RATE_ACTIVE_COLUMNS, required=False
            )
            direct_enterprise = _resolve_column(
                cursor, "rate_current", RATE_ENTERPRISE_COLUMNS, required=False
            )

            active_predicate = SQL("AND rate.{} ").format(Identifier(active_column)) \
                if active_column else SQL("")

            if direct_enterprise:
                query = SQL("""
                    SELECT DISTINCT
                        rate.id::text AS rate_id,
                        trim(rate.{name})::text AS rate_name
                    FROM rate_current rate
                    WHERE rate.enterprise_id::text = %(enterprise_id)s
                      AND nullif(trim(rate.{name}), '') IS NOT NULL
                      {active}
                    ORDER BY rate_name
                """).format(name=Identifier(name_column), active=active_predicate)
            else:
                query = SQL("""
                    SELECT DISTINCT
                        rate.id::text AS rate_id,
                        trim(rate.{name})::text AS rate_name
                    FROM rate_current rate
                    JOIN service_current service ON service.id = rate.service_id
                    WHERE service.enterprise_id::text = %(enterprise_id)s
                      AND nullif(trim(rate.{name}), '') IS NOT NULL
                      {active}
                    ORDER BY rate_name
                """).format(name=Identifier(name_column), active=active_predicate)

            cursor.execute(query, {"enterprise_id": str(enterprise_id)})
            return [
                {"id": row["rate_id"], "name": row["rate_name"]}
                for row in cursor.fetchall()
            ]


def list_channels(enterprise_id):
    """Distinct booking channels for one hotel, from the reservation origin.

    Mews has no channel entity; the closest thing is the reservation's origin
    (ChannelManager, Connector, Commander, ...) or the channel manager name.
    Returns an empty list when no such column exists, so the picker degrades to
    free text rather than failing the whole page.
    """
    with get_export_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            channel_column = _resolve_column(
                cursor, "reservation_current", CHANNEL_COLUMNS, required=False
            )
            if channel_column is None:
                logging.info(
                    "No channel-like column on reservation_current; "
                    "channel matches stay free text"
                )
                return []

            query = SQL("""
                SELECT DISTINCT trim(reservation.{channel})::text AS channel_name
                FROM reservation_current reservation
                JOIN service_current service ON service.id = reservation.service_id
                WHERE service.enterprise_id::text = %(enterprise_id)s
                  AND nullif(trim(reservation.{channel}), '') IS NOT NULL
                ORDER BY channel_name
            """).format(channel=Identifier(channel_column))

            cursor.execute(query, {"enterprise_id": str(enterprise_id)})
            return [
                {"id": row["channel_name"], "name": row["channel_name"]}
                for row in cursor.fetchall()
            ]


def list_cleaning_categories(enterprise_id):
    """Room categories for one hotel with every servable occupancy.

    Mews ResourceCategory carries Capacity (standard occupancy) and
    ExtraCapacity (extra beds). A category serving 2 + 1 produces occupancy
    steps 1, 2 and 3 - one cleaning row each, because linen and minutes differ
    per occupancy.

    Categories come back in the Mews ordering (ResourceCategory.Ordering), not
    alphabetically: that is the order the property recognises, and every screen
    listing space categories uses it. A mirror without the ordering column
    falls back to the name, which is the previous behaviour.
    """
    with get_export_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            name_column = _resolve_column(
                cursor, "resource_category_current", CATEGORY_NAME_COLUMNS
            )
            capacity_column = _resolve_column(
                cursor, "resource_category_current", CATEGORY_CAPACITY_COLUMNS
            )
            extra_column = _resolve_column(
                cursor,
                "resource_category_current",
                CATEGORY_EXTRA_CAPACITY_COLUMNS,
                required=False,
            )
            ordering_column = _resolve_column(
                cursor,
                "resource_category_current",
                CATEGORY_ORDERING_COLUMNS,
                required=False,
            )
            extra_expression = (
                SQL("coalesce(category.{}, 0)").format(Identifier(extra_column))
                if extra_column else SQL("0")
            )
            ordering_expression = (
                SQL("coalesce(category.{}, {})").format(
                    Identifier(ordering_column), SQL(str(UNORDERED_CATEGORY_RANK))
                )
                if ordering_column else SQL(str(UNORDERED_CATEGORY_RANK))
            )
            if ordering_column is None:
                logging.info(
                    "No Mews ordering column on resource_category_current "
                    "(tried %s); space categories fall back to name order",
                    list(CATEGORY_ORDERING_COLUMNS),
                )

            query = SQL("""
                SELECT
                    category.id::text AS category_id,
                    trim(category.{name})::text AS category_name,
                    coalesce(category.{capacity}, 0)::int AS capacity,
                    {extra}::int AS extra_capacity,
                    {ordering}::int AS category_ordering
                FROM resource_category_current category
                WHERE category.enterprise_id::text = %(enterprise_id)s
                  AND category.type = 'Room'
                  AND category.is_active
                  AND nullif(trim(category.{name}), '') IS NOT NULL
                ORDER BY category_ordering, category_name
            """).format(
                name=Identifier(name_column),
                capacity=Identifier(capacity_column),
                extra=extra_expression,
                ordering=ordering_expression,
            )

            cursor.execute(query, {"enterprise_id": str(enterprise_id)})
            rows = cursor.fetchall()

    categories = []
    for row in rows:
        total = int(row["capacity"] or 0) + int(row["extra_capacity"] or 0)
        if total < 1:
            # A category that cannot hold a guest has nothing to clean per head.
            continue
        categories.append({
            "categoryId": row["category_id"],
            "categoryName": row["category_name"],
            "capacity": int(row["capacity"] or 0),
            "extraCapacity": int(row["extra_capacity"] or 0),
            "ordering": int(row.get("category_ordering") or UNORDERED_CATEGORY_RANK),
            "occupancies": list(range(1, total + 1)),
        })
    return categories


def fetch_cost_sources(enterprise_id):
    """Everything the Cost Input pickers need for one hotel."""
    return {
        "rates": list_rates(enterprise_id),
        "channels": list_channels(enterprise_id),
        "cleaningCategories": list_cleaning_categories(enterprise_id),
    }
