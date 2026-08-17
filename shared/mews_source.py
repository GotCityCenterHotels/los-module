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


# "Does this travel agency name contain this term?", as one rule.
#
# A travel agency's name is written differently everywhere it is written:
# Booking.com, BOOKING.COM, "Booking com", "Booking.com B.V.". An operator typing
# one of those into a Cost Input filter means all of them, and a plain substring
# test on the raw text means only the one they happened to type - so "booking.com"
# silently matched nothing at a property whose mirror spells it "Booking com".
#
# Both sides are folded to letters and digits only, in lower case, before the
# substring test. That absorbs case, spacing, punctuation and a trailing company
# form in one step: booking.com, Booking Com and BOOKING.COM all fold to
# "bookingcom", and "Booking.com B.V." folds to "bookingcombv", which contains it.
#
# What is removed is spacing and punctuation, and nothing else. The tempting
# shorter pattern - drop everything that is not [:alnum:] - is ctype-dependent:
# under the C locale it is ASCII-only, so "Hôtel Diva" would fold to "hteldiva"
# and stop matching "hotel diva" as well as "hôtel diva". Naming only the two
# classes to remove keeps every accented letter whatever the server's locale,
# which matters for most of the Nordic and French names in this source. Case is
# handled by lower(), which is collation-aware and does fold accented letters.
#
# Three callers have to agree on this or the editor shows matches the cost never
# applies: the agency picker and the matching-rate picker in
# services/cost_source_service.py, and the distribution cost query in
# queries/cost_data.py. There is deliberately no second implementation of it in
# Python - a rule that must be identical in three places does not get a fourth
# copy that could drift.
AGENCY_FOLD_PATTERN = "[[:space:][:punct:]]+"


def agency_fold_text(expression):
    """SQL folding one agency name or search term to its comparable form.

    Takes and returns plain SQL text, so the caller can compose it either into a
    psycopg SQL() template or into a query string.
    """
    return (
        "lower(regexp_replace(coalesce(" + expression + ", ''), "
        f"'{AGENCY_FOLD_PATTERN}', '', 'g'))"
    )


def agency_contains_text(name_expression, term_expression):
    """SQL testing whether a folded agency name contains a folded term."""
    return (
        f"strpos({agency_fold_text(name_expression)}, "
        f"{agency_fold_text(term_expression)}) > 0"
    )
