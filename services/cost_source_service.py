"""Read-only lookups against integration_db for the Cost Input pickers.

integration_db is read-only at both the role and session level (see
shared/db.py), and everything here is a bounded SELECT.

The source mirrors the Mews Connector API, but the ETL flattens localized text
into single columns and the exact naming varies per table (resource categories
expose "space_name", not "names"). Rather than hard-coding a guess, each column
is resolved once from information_schema against a candidate list and cached for
the process. A missing column then produces an explicit message naming the
table and the candidates tried, instead of a bare UndefinedColumn at runtime.

Everything one page load needs travels on a single pooled statement-bounded
connection: opening one TLS+SCRAM connection per lookup cost more than the
lookups themselves.
"""

import logging
import os

from datetime import date, timedelta
from threading import Lock
from time import monotonic

from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier, Literal

from shared.db import HTTP_EXPORT_STATEMENT_TIMEOUT_MS, get_export_connection
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

# Mews Reservation.Origin is an enum (Distributor, ChannelManager, Connector,
# Commander, Import). The distribution rulebook groups on it, and the picker
# reads the same column the channel picker already reads.
RESERVATION_ORIGIN_COLUMNS = CHANNEL_COLUMNS

# Mews Reservation.TravelAgencyId is a foreign key to a Company - Mews has no
# separate travel-agency entity - so the searchable name lives on the company
# table. A mirror that kept only CompanyId is handled by the second list.
RESERVATION_TRAVEL_AGENCY_COLUMNS = (
    "travel_agency_id", "travelagency_id", "travel_agency_company_id",
    "agency_id", "travel_agent_id",
)
RESERVATION_COMPANY_COLUMNS = ("company_id", "corporate_company_id", "account_id")

# The rate the reservation is actually sold on. Needed to answer "which rates
# occur under these filters" rather than "which rates exist".
RESERVATION_RATE_COLUMNS = ("rate_id", "reservation_rate_id", "current_rate_id")

# The table the travel-agency foreign key points at, in order of likelihood.
AGENCY_TABLES = (
    "company_current", "companies_current", "travel_agency_current",
    "agency_current", "account_current", "customer_current",
)
AGENCY_NAME_COLUMNS = (
    "name", "company_name", "legal_name", "display_name", "names", "short_name",
)

# A reservation that has not been seen in two years is not a live picker
# option, and an unbounded scan of reservation_current was what made the Cost
# Input page take tens of seconds to load. Everything reservation-derived is
# bounded to this window unless the caller narrows it further.
SOURCE_WINDOW_DAYS = int(os.environ.get("COST_SOURCE_WINDOW_DAYS", "730"))
PICKER_LIMIT = int(os.environ.get("COST_SOURCE_PICKER_LIMIT", "500"))

# The picker data for one hotel changes at most daily. Recomputing it on every
# page load is what the cache headers already assume; this is the server-side
# half of the same bargain, and it also covers the second worker that never
# sees the browser cache.
SOURCE_CACHE_TTL_SECONDS = 600
_source_cache = {}
_source_cache_lock = Lock()


def _table_columns(cursor, table_name):
    """Column names present on a source table, cached per process.

    The probe is restricted to the schemas the session actually resolves. An
    unqualified table_name matched every schema, so a same-named table in
    staging could union its columns into this set and _resolve_column would
    then "resolve" a column that does not exist on the table being queried -
    exactly the UndefinedColumn this module exists to prevent.
    """
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
              AND table_schema = ANY(current_schemas(false))
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


def _resolve_table(cursor, candidates):
    """The first candidate table that exists, or None.

    _resolve_column can only answer "which column on this table"; the agency
    lookup also has to guess the table, because no other code in this
    application has ever read it.
    """
    for candidate in candidates:
        if _table_columns(cursor, candidate):
            return candidate
    return None


def _reset_column_cache():
    """Test seam - the cache is keyed by table name only."""
    with _column_lock:
        _column_cache.clear()
    with _source_cache_lock:
        _source_cache.clear()


class _Session:
    """One integration_db connection, opened lazily and shared by the lookups.

    Every lookup used to open its own connection: three TLS handshakes and
    three SCRAM exchanges per page load, ~60-150ms each against Azure Postgres,
    all serialized inside one request before a single row was read.
    """

    def __init__(self, cursor=None):
        self._cursor = cursor
        self._connection = None
        self._owns = cursor is None

    def __enter__(self):
        if self._cursor is None:
            self._connection = get_export_connection(
                statement_timeout_ms=HTTP_EXPORT_STATEMENT_TIMEOUT_MS
            )
            self._connection.__enter__()
            self._cursor = self._connection.cursor(row_factory=dict_row)
        return self._cursor

    def __exit__(self, *exception):
        if self._owns and self._connection is not None:
            return self._connection.__exit__(*exception)
        return False


def _window_start(days=None):
    return date.today() - timedelta(days=days or SOURCE_WINDOW_DAYS)


def contains_pattern(term):
    """ILIKE '%term%' with the LIKE metacharacters in the user's term escaped.

    A search for "50%" must look for a literal per cent sign, not "anything".
    """
    escaped = (
        str(term or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


# Reservations for one hotel, bounded to the recent window.
#
# `service.enterprise_id::text = %s` is an expression predicate that an ordinary
# index cannot serve, and casting the parameter instead would be faster - but
# the mirror types this key as uuid on some deployments and text on others, and
# there is no way to know which without a probe the unit tests cannot stub. It
# is left as-is deliberately: service_current holds a handful of rows per hotel,
# so the scan there is free. The cost that mattered was the join into
# reservation_current, and that is what start_utc now bounds.
_STAY_SCOPE_HEAD = SQL("""
    FROM reservation_current reservation
    JOIN service_current service
      ON service.id = reservation.service_id
     AND service.name = 'Stay'
""")
_STAY_SCOPE_TAIL = SQL("""
    WHERE service.enterprise_id::text = %(enterprise_id)s
      AND reservation.start_utc >= %(window_start)s
""")
_STAY_SCOPE = SQL("{}{}").format(_STAY_SCOPE_HEAD, _STAY_SCOPE_TAIL)


def list_rates(enterprise_id, cursor=None):
    """Active rates for one hotel.

    Mews Rate has no EnterpriseId - it hangs off ServiceId - so the join runs
    through service_current unless the mirror denormalised enterprise_id onto
    the rate itself.
    """
    with _Session(cursor) as source:
        name_column = _resolve_column(source, "rate_current", RATE_NAME_COLUMNS)
        active_column = _resolve_column(
            source, "rate_current", RATE_ACTIVE_COLUMNS, required=False
        )
        direct_enterprise = _resolve_column(
            source, "rate_current", RATE_ENTERPRISE_COLUMNS, required=False
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

        source.execute(query, {"enterprise_id": str(enterprise_id)})
        return [
            {"id": row["rate_id"], "name": row["rate_name"]}
            for row in source.fetchall()
        ]


def list_channels(enterprise_id, cursor=None):
    """Distinct booking channels for one hotel, from the reservation origin.

    Mews has no channel entity; the closest thing is the reservation's origin
    (ChannelManager, Connector, Commander, ...) or the channel manager name.
    Returns an empty list when no such column exists, so the picker degrades to
    free text rather than failing the whole page.

    Bounded to the recent window. This query used to read every reservation the
    hotel has ever had - hundreds of thousands of rows, aggregated down to a
    handful of distinct strings - on every single page load, and it was the
    single largest cost in loading Cost Input.
    """
    with _Session(cursor) as source:
        channel_column = _resolve_column(
            source, "reservation_current", CHANNEL_COLUMNS, required=False
        )
        if channel_column is None:
            logging.info(
                "No channel-like column on reservation_current; "
                "channel matches stay free text"
            )
            return []

        query = SQL("""
            SELECT trim(reservation.{channel})::text AS channel_name
            {scope}
              AND nullif(trim(reservation.{channel}), '') IS NOT NULL
            GROUP BY 1
            ORDER BY channel_name
            LIMIT {limit}
        """).format(
            channel=Identifier(channel_column),
            scope=_STAY_SCOPE,
            limit=Literal(PICKER_LIMIT),
        )

        source.execute(query, {
            "enterprise_id": str(enterprise_id),
            "window_start": _window_start(),
        })
        return [
            {"id": row["channel_name"], "name": row["channel_name"]}
            for row in source.fetchall()
        ]


def list_cleaning_categories(enterprise_id, cursor=None):
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
    with _Session(cursor) as source:
        name_column = _resolve_column(
            source, "resource_category_current", CATEGORY_NAME_COLUMNS
        )
        capacity_column = _resolve_column(
            source, "resource_category_current", CATEGORY_CAPACITY_COLUMNS
        )
        extra_column = _resolve_column(
            source,
            "resource_category_current",
            CATEGORY_EXTRA_CAPACITY_COLUMNS,
            required=False,
        )
        ordering_column = _resolve_column(
            source,
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

        source.execute(query, {"enterprise_id": str(enterprise_id)})
        rows = source.fetchall()

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


def list_origins(enterprise_id, cursor=None):
    """Distinct reservation origins for one hotel, with how often each occurs.

    The distribution rulebook's top level groups on origin, so the picker has to
    offer the origins this property actually books through - not a hard-coded
    Mews enum, which would list values the property never sees.
    """
    with _Session(cursor) as source:
        origin_column = _resolve_column(
            source, "reservation_current", RESERVATION_ORIGIN_COLUMNS, required=False
        )
        if origin_column is None:
            logging.info(
                "No origin-like column on reservation_current (tried %s); "
                "origin groups stay free text",
                list(RESERVATION_ORIGIN_COLUMNS),
            )
            return []

        query = SQL("""
            SELECT trim(reservation.{origin})::text AS origin_name,
                   count(*)::bigint AS reservation_count
            {scope}
              AND nullif(trim(reservation.{origin}), '') IS NOT NULL
            GROUP BY 1
            ORDER BY origin_name
            LIMIT {limit}
        """).format(
            origin=Identifier(origin_column),
            scope=_STAY_SCOPE,
            limit=Literal(PICKER_LIMIT),
        )
        source.execute(query, {
            "enterprise_id": str(enterprise_id),
            "window_start": _window_start(),
        })
        return [
            {
                "id": row["origin_name"],
                "name": row["origin_name"],
                "reservationCount": int(row["reservation_count"]),
            }
            for row in source.fetchall()
        ]


def _agency_join(source):
    """(reservation FK column, agency table, agency name column) or Nones."""
    agency_fk = _resolve_column(
        source,
        "reservation_current",
        RESERVATION_TRAVEL_AGENCY_COLUMNS + RESERVATION_COMPANY_COLUMNS,
        required=False,
    )
    if agency_fk is None:
        return None, None, None
    agency_table = _resolve_table(source, AGENCY_TABLES)
    if agency_table is None:
        return agency_fk, None, None
    agency_name = _resolve_column(
        source, agency_table, AGENCY_NAME_COLUMNS, required=False
    )
    if agency_name is None:
        return agency_fk, agency_table, None
    return agency_fk, agency_table, agency_name


def list_travel_agencies(enterprise_id, search="", origins=None, cursor=None):
    """Travel agencies this hotel has reservations from, filtered by a
    case-insensitive "contains" search.

    Mews has no travel-agency entity: Reservation.TravelAgencyId points at a
    Company, so the searchable name comes from the company table. Returns an
    empty list rather than failing when the mirror carries neither, so the
    subgroup filter degrades to a typed value that is matched at cost time.
    """
    with _Session(cursor) as source:
        agency_fk, agency_table, agency_name = _agency_join(source)
        if not (agency_fk and agency_table and agency_name):
            logging.info(
                "No travel-agency link on reservation_current "
                "(fk=%s table=%s name=%s); agency filters stay free text",
                agency_fk, agency_table, agency_name,
            )
            return []

        origin_column = _resolve_column(
            source, "reservation_current", RESERVATION_ORIGIN_COLUMNS, required=False
        )
        query = SQL("""
            SELECT trim(agency.{agency_name})::text AS agency_name,
                   count(*)::bigint AS reservation_count
            {scope}
              AND reservation.{agency_fk} IS NOT NULL
              {origin_predicate}
              AND nullif(trim(agency.{agency_name}), '') IS NOT NULL
              AND (
                  %(agency_pattern)s::text IS NULL
                  OR agency.{agency_name} ILIKE %(agency_pattern)s ESCAPE '\\'
              )
            GROUP BY 1
            ORDER BY reservation_count DESC, agency_name
            LIMIT {limit}
        """).format(
            agency_name=Identifier(agency_name),
            agency_fk=Identifier(agency_fk),
            scope=_scope_with_agency(agency_table, agency_fk),
            origin_predicate=_origin_predicate(origin_column),
            limit=Literal(PICKER_LIMIT),
        )
        source.execute(query, {
            "enterprise_id": str(enterprise_id),
            "window_start": _window_start(),
            "agency_pattern": contains_pattern(search) if str(search or "").strip() else None,
            "origins": list(origins) if origins else None,
        })
        return [
            {
                "id": row["agency_name"],
                "name": row["agency_name"],
                "reservationCount": int(row["reservation_count"]),
            }
            for row in source.fetchall()
        ]


def _scope_with_agency(agency_table, agency_fk):
    """The stay scope with the company table joined in for the name search."""
    return SQL(
        "{head} JOIN {agency_table} agency "
        "ON agency.id = reservation.{agency_fk} {tail}"
    ).format(
        head=_STAY_SCOPE_HEAD,
        agency_table=Identifier(agency_table),
        agency_fk=Identifier(agency_fk),
        tail=_STAY_SCOPE_TAIL,
    )


def _origin_predicate(origin_column):
    """AND clause narrowing to a set of origins, or nothing.

    A NULL array means "no origin filter", so one statement serves every
    combination and the planner keeps one plan for it.
    """
    if origin_column is None:
        return SQL("")
    return SQL(
        "AND (%(origins)s::text[] IS NULL "
        "OR trim(reservation.{}) = ANY(%(origins)s::text[]))"
    ).format(Identifier(origin_column))


def list_matching_rates(enterprise_id, origins=None, agencySearch="", cursor=None):
    """Rates that reservations under these filters were actually sold on.

    The deepest level of the distribution rulebook assigns a percentage to
    named rates, and offering every rate on the property would bury the handful
    that can occur under the chosen origin and agency. Returns a `filtered`
    flag so the caller can say plainly whether the narrowing happened, rather
    than presenting the full list as if it were the filtered one.
    """
    with _Session(cursor) as source:
        rate_fk = _resolve_column(
            source, "reservation_current", RESERVATION_RATE_COLUMNS, required=False
        )
        rate_name_column = _resolve_column(
            source, "rate_current", RATE_NAME_COLUMNS, required=False
        )
        if rate_fk is None or rate_name_column is None:
            logging.info(
                "Reservations carry no usable rate link (rate_fk=%s rate_name=%s); "
                "falling back to every rate on the property",
                rate_fk, rate_name_column,
            )
            return {"rates": list_rates(enterprise_id, cursor=source), "filtered": False}

        origin_column = _resolve_column(
            source, "reservation_current", RESERVATION_ORIGIN_COLUMNS, required=False
        )
        agency_fk, agency_table, agency_name = _agency_join(source)
        wants_agency = bool(str(agencySearch or "").strip())
        agency_available = bool(agency_fk and agency_table and agency_name)

        if wants_agency and agency_available:
            scope = _scope_with_agency(agency_table, agency_fk)
            agency_predicate = SQL(
                "AND agency.{} ILIKE %(agency_pattern)s ESCAPE '\\'"
            ).format(Identifier(agency_name))
        else:
            scope = _STAY_SCOPE
            agency_predicate = SQL("")

        query = SQL("""
            WITH scoped_rates AS MATERIALIZED (
                SELECT reservation.{rate_fk} AS rate_id,
                       count(*)::bigint AS reservation_count
                {scope}
                  AND reservation.{rate_fk} IS NOT NULL
                  {origin_predicate}
                  {agency_predicate}
                GROUP BY 1
            )
            SELECT rate.id::text AS rate_id,
                   trim(rate.{rate_name})::text AS rate_name,
                   scoped.reservation_count
            FROM scoped_rates scoped
            JOIN rate_current rate ON rate.id = scoped.rate_id
            WHERE nullif(trim(rate.{rate_name}), '') IS NOT NULL
            ORDER BY rate_name
            LIMIT {limit}
        """).format(
            rate_fk=Identifier(rate_fk),
            rate_name=Identifier(rate_name_column),
            scope=scope,
            origin_predicate=_origin_predicate(origin_column),
            agency_predicate=agency_predicate,
            limit=Literal(PICKER_LIMIT),
        )
        source.execute(query, {
            "enterprise_id": str(enterprise_id),
            "window_start": _window_start(),
            "origins": list(origins) if origins else None,
            "agency_pattern": contains_pattern(agencySearch) if wants_agency else None,
        })
        rates = [
            {
                "id": row["rate_id"],
                "name": row["rate_name"],
                "reservationCount": int(row["reservation_count"]),
            }
            for row in source.fetchall()
        ]

    return {
        "rates": rates,
        "filtered": True,
        # An agency term the mirror cannot honour must be reported, not
        # silently dropped: a full rate list returned as if it were filtered
        # reads as truth.
        "agencyFilterApplied": bool(wants_agency and agency_available),
        "originFilterApplied": bool(origins and origin_column is not None),
    }


def _fetch_cost_sources_uncached(enterprise_id):
    with _Session() as source:
        rates = list_rates(enterprise_id, cursor=source)
        categories = list_cleaning_categories(enterprise_id, cursor=source)
        origins = list_origins(enterprise_id, cursor=source)
        origin_column = _resolve_column(
            source, "reservation_current", RESERVATION_ORIGIN_COLUMNS, required=False
        )
        rate_fk = _resolve_column(
            source, "reservation_current", RESERVATION_RATE_COLUMNS, required=False
        )
        agency_fk, agency_table, agency_name = _agency_join(source)
    return {
        "rates": rates,
        # A "channel" was only ever the reservation origin under another name -
        # RESERVATION_ORIGIN_COLUMNS and CHANNEL_COLUMNS are the same list - so
        # running list_channels here meant scanning reservation_current twice
        # per page load for the same rows. That scan is the single largest cost
        # in loading Cost Input, so it is now run once and shared.
        "channels": [
            {"id": origin["name"], "name": origin["name"]} for origin in origins
        ],
        "cleaningCategories": categories,
        "origins": origins,
        # The Cost Input page says which filters this mirror can honour rather
        # than offering a search box that silently matches nothing.
        "capabilities": {
            "origin": origin_column is not None,
            "travelAgency": bool(agency_fk and agency_table and agency_name),
            "rateFromReservations": rate_fk is not None,
        },
    }


def fetch_cost_sources(enterprise_id):
    """Everything the Cost Input pickers need for one hotel.

    Memoized per worker: this data changes at most daily, and recomputing it on
    every page load is the difference between an instant editor and one that
    waits on integration_db.
    """
    key = str(enterprise_id)
    now = monotonic()
    with _source_cache_lock:
        cached = _source_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

    payload = _fetch_cost_sources_uncached(enterprise_id)
    with _source_cache_lock:
        _source_cache[key] = (now + SOURCE_CACHE_TTL_SECONDS, payload)
    return payload
