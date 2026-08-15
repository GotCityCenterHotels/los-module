"""Shared knowledge about the Mews mirror in integration_db.

Space (room) categories are listed on more than one screen, and they must
appear in the same order everywhere: the order Mews itself defines, from
ResourceCategory.Ordering. This module is the single place that knows which
mirrored column carries that field, so adding a synonym fixes every list at
once rather than one screen at a time.

The ETL flattens Mews entities into columns whose exact naming varies per
table, so the column is resolved once against information_schema and cached
for the process, the same way services/cost_source_service.py resolves the
category name and capacity columns.
"""

from threading import Lock

from psycopg.sql import SQL, Identifier


# Mews ResourceCategory.Ordering, in order of likelihood. The first column
# present on the table wins.
CATEGORY_ORDERING_COLUMNS = (
    "ordering", "category_ordering", "sort_order", "display_order",
    "order_index", "position",
)

# A category with no ordering value must not jump to the front of the list;
# it sorts after everything Mews has ordered, by name.
UNORDERED_CATEGORY_RANK = 2147483647


_column_cache = {}
_column_lock = Lock()


def table_columns(cursor, table_name):
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
        columns = {
            row["column_name"] if isinstance(row, dict) else row[0]
            for row in cursor.fetchall()
        }
        _column_cache[table_name] = columns
        return columns


def resolve_optional_column(cursor, table_name, candidates):
    """First candidate column present on the table, or None."""
    columns = table_columns(cursor, table_name)
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def category_ordering_expression(cursor, table_name, alias):
    """SQL for the Mews ordering of one space-category table.

    Returns a composable expression that is always an integer: the mirrored
    ordering column when it exists, and the unordered rank when it does not,
    so callers can sort by it unconditionally.
    """
    column = resolve_optional_column(
        cursor, table_name, CATEGORY_ORDERING_COLUMNS
    )
    if column is None:
        return SQL("{}::int").format(SQL(str(UNORDERED_CATEGORY_RANK))), None
    return (
        SQL("coalesce({}.{}, {})::int").format(
            Identifier(alias),
            Identifier(column),
            SQL(str(UNORDERED_CATEGORY_RANK)),
        ),
        column,
    )


def reset_column_cache():
    """Test seam - the cache is keyed by table name only."""
    with _column_lock:
        _column_cache.clear()
