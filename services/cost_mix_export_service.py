"""Export statements for the two reservation-level cost mixes.

The Cost Data page charges cleaning per departure and distribution as a
percentage of room revenue. Until these two datasets existed it had only
hotel-per-day totals, so it blended every configured cleaning row into a single
mean per departure and could apply nothing but the fallback distribution
percentage - and said so, in two flags on the statement that nobody could act on.

Both datasets are a MIX, not a level:

  * departure_mix_data splits a day's departures across (room category, guests);
  * distribution_mix_data splits a day's room revenue across (origin, travel
    agency, rate).

The authoritative totals stay in functions.arr_dep_data and
functions.room_revenue_night_data, and the statement keeps reading them, so a mix
that is slightly out cannot move a total - only how that total is apportioned.

Every source column here is resolved from information_schema rather than
assumed, the same way services/cost_source_service.py resolves the picker
columns. None of them is read anywhere else in this application and the Mews
mirror's naming varies per deployment, so a builder that cannot find what it
needs returns None: the dataset imports nothing, the page keeps its previous
figure and its flag, and the nightly import does not fail. That is deliberately
the failure mode - an UndefinedColumn here would take the whole cost import down
with it, including the five datasets that were working.
"""

import logging
import os

from datetime import date, timedelta

from psycopg.sql import SQL, Identifier, Literal

from services.cost_source_service import (
    CATEGORY_NAME_COLUMNS,
    RATE_NAME_COLUMNS,
    RESERVATION_ORIGIN_COLUMNS,
    RESERVATION_RATE_COLUMNS,
    CostSourceUnavailableError,
    _agency_join,
    _resolve_column,
    _table_columns,
    table_identifier,
)
from shared.mews_source import rate_name_lateral


# Reservation-level exports are the two most expensive statements in the cost
# import, so they are bounded rather than reading all history like the five
# hotel-per-day exports do. The window matches COST_SOURCE_WINDOW_DAYS, so the
# mix covers exactly the period the Cost Input pickers offer values from; a
# period older than this falls back to the previous behaviour and says so.
MIX_WINDOW_DAYS = int(os.environ.get("COST_MIX_WINDOW_DAYS", "730"))

# The departure mix is derived from the same relation as the departure TOTAL it
# apportions: staging.room_nights_source, filtered on canceled_utc, with the
# departure date taken from end_utc in Stockholm time and one departure counted
# per distinct reservation. That is sql/export/arr_dep_data.sql exactly, only
# partitioned further - so the mix sums to total_departures by construction
# instead of by coincidence.
#
# It was built from reservation_current first, which was wrong: a mix drawn from
# a different relation than the total can disagree with it, and a weighting that
# disagrees with the thing it weights is not a weighting.
NIGHTS_TABLE = "staging.room_nights_source"
NIGHTS_END_COLUMNS = ("end_utc",)
NIGHTS_CANCELED_COLUMNS = ("canceled_utc", "cancelled_utc")
NIGHTS_HOTEL_COLUMNS = ("hotel_name",)
# The reservation this room night belongs to, for reaching its category and guest
# count. room_nights_source carries both this and `number` - the human-readable
# confirmation number - so the key is resolved rather than assumed.
NIGHTS_RESERVATION_KEY_COLUMNS = (
    "reservation_id", "reservation_key", "service_order_id",
)
# Mews Reservation.RequestedResourceCategoryId / AssignedResourceCategoryId. The
# cleaning rulebook is written per room category, so this is what binds a
# departure to a configured row.
RESERVATION_CATEGORY_COLUMNS = (
    "requested_resource_category_id", "requested_category_id",
    "assigned_resource_category_id", "resource_category_id", "category_id",
)
# Mews Reservation.AdultCount and .ChildCount, where the mirror flattened them
# into two integers. Kept as two separate lists, and the person-count column below
# is only consulted when neither is present: adding a total that already includes
# children to a child count would double the occupancy and cost every family room
# at the wrong configured row.
RESERVATION_ADULT_COLUMNS = ("adult_count", "adults", "number_of_adults")
RESERVATION_CHILD_COLUMNS = ("child_count", "children", "number_of_children")

# Mews Reservation.PersonCounts, which is not a number - it is a list with one
# entry per age category:
#
#   [{"Count": 1, "AgeCategoryId": "2d7a…"}, {"Count": 3, "AgeCategoryId": "2df…"}]
#
# so the guests in the room are the SUM of its Counts, four in that example. Every
# age category counts: a child in the room is still a bed made up and a towel
# changed, and the occupancies the Cost Input editor offers come from a category's
# capacity plus its extra beds, which is a head count too.
#
# Listed before the scalar candidates because this is the shape Mews actually
# publishes; the scalars are for a mirror that flattened it.
RESERVATION_PERSON_COLUMNS = (
    "person_counts", "person_count", "guest_count", "total_person_count",
    "persons", "occupancy",
)

# Which of those two readings a column gets is decided by its declared type, not
# by its name. A mirror that flattened PersonCounts into an integer still called
# person_counts would otherwise be read as an empty list and every room costed at
# one guest - wrong, and silently so.
NUMERIC_COLUMN_TYPES = frozenset({
    "smallint", "integer", "bigint", "numeric", "decimal", "real",
    "double precision",
})
# Parity with sql/export/arr_dep_data.sql, which counts a departure whenever
# canceled_utc is null and applies no other state filter. The mix has to agree
# with the total it apportions, so it filters on exactly the same thing.
RESERVATION_CANCELED_COLUMNS = ("canceled_utc", "cancelled_utc")
# order_item_current.Type. 'SpaceOrder' is the accommodation charge, which is the
# revenue a distribution fee is actually charged on; without a type column the
# filter is dropped rather than guessed at.
ORDER_ITEM_TYPE_COLUMNS = ("type", "item_type")

STOCKHOLM = SQL("AT TIME ZONE 'Europe/Stockholm'")


def _window_start():
    return date.today() - timedelta(days=MIX_WINDOW_DAYS)


def _column_type(source, table_name, column_name):
    """The declared type of one source column, lower-cased, or None.

    _resolve_column answers "which of these names exists"; this answers "and what
    is it", which is what decides whether a person count is a number to read or a
    list to sum.
    """
    schema, bare = table_name.rpartition(".")[0], table_name.rpartition(".")[2]
    if schema:
        source.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_name = %s AND table_schema = %s AND column_name = %s
            """,
            (bare, schema, column_name),
        )
    else:
        source.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
              AND table_schema = ANY(current_schemas(false))
            """,
            (bare, column_name),
        )
    rows = source.fetchall()
    if not rows:
        return None
    row = rows[0]
    value = row["data_type"] if isinstance(row, dict) else row[0]
    return str(value or "").lower()


def _person_counts_total(reference):
    """The sum of a Mews PersonCounts list, as SQL.

    Cast to jsonb rather than assumed to be jsonb: the mirror may land it as json,
    or as text holding the same document, and all three cast the same way.

    Guarded by jsonb_typeof, because jsonb_array_elements raises on anything that
    is not an array - and a single malformed row must not fail the whole import.
    The Count key is read in both casings the ETL might have produced.
    """
    return SQL("""(
                    SELECT sum(coalesce(
                        nullif(entry ->> 'Count', ''),
                        nullif(entry ->> 'count', ''),
                        '0'
                    )::int)
                    FROM jsonb_array_elements(
                        CASE WHEN jsonb_typeof({reference}::jsonb) = 'array'
                             THEN {reference}::jsonb
                             ELSE '[]'::jsonb END
                    ) AS entry
                )""").format(reference=reference)


def _occupancy_expression(source, table_name, alias):
    """Guests in the room, however this mirror records them.

    Floored at one: a reservation with no counts recorded is still a room that was
    turned over, and costing it at zero minutes would understate the day. A
    PersonCounts list that is absent, empty or malformed sums to nothing, so it
    lands on one guest through the same floor.
    """
    adult = _resolve_column(
        source, table_name, RESERVATION_ADULT_COLUMNS, required=False
    )
    child = _resolve_column(
        source, table_name, RESERVATION_CHILD_COLUMNS, required=False
    )
    if adult:
        parts = SQL("coalesce({}.{}, 0)").format(
            Identifier(alias), Identifier(adult)
        )
        if child:
            parts = SQL("{} + coalesce({}.{}, 0)").format(
                parts, Identifier(alias), Identifier(child)
            )
        return SQL("greatest(1, ({})::int)").format(parts)

    for candidate in RESERVATION_PERSON_COLUMNS:
        column = _resolve_column(
            source, table_name, (candidate,), required=False
        )
        if column is None:
            continue
        reference = SQL("{}.{}").format(Identifier(alias), Identifier(column))
        data_type = _column_type(source, table_name, column)
        total = (
            SQL("coalesce({}, 0)").format(reference)
            if data_type in NUMERIC_COLUMN_TYPES
            else SQL("coalesce({}, 0)").format(_person_counts_total(reference))
        )
        logging.info(
            "Departure mix reads guests from %s.%s (%s) as %s",
            table_name, column, data_type or "unknown type",
            "a number" if data_type in NUMERIC_COLUMN_TYPES else "a PersonCounts list",
        )
        return SQL("greatest(1, ({})::int)").format(total)
    return None


def _canceled_predicate(source):
    column = _resolve_column(
        source, "reservation_current", RESERVATION_CANCELED_COLUMNS, required=False
    )
    if column is None:
        logging.info(
            "reservation_current carries no cancellation column (tried %s); the "
            "cost mixes count every reservation",
            list(RESERVATION_CANCELED_COLUMNS),
        )
        return SQL("")
    return SQL("AND reservation.{} IS NULL").format(Identifier(column))


def _departure_dimensions(source):
    """Where the room category and the guest count come from.

    Preferred on staging.room_nights_source itself, because that avoids a join
    into reservation_current across hundreds of thousands of rows. Falls back to
    reservation_current, which is where Mews puts both. Returns the two
    expressions and whether the reservation join is needed for them.
    """
    for table, alias in ((NIGHTS_TABLE, "nights"), ("reservation_current", "reservation")):
        category_fk = _resolve_column(
            source, table, RESERVATION_CATEGORY_COLUMNS, required=False
        )
        occupancy = _occupancy_expression(source, table, alias)
        if category_fk and occupancy:
            return category_fk, occupancy, alias == "reservation"
    # Split across the two: the category on one, the counts on the other. Rare,
    # but the join is already paid for in that case so there is nothing to lose.
    category_fk = _resolve_column(
        source, "reservation_current", RESERVATION_CATEGORY_COLUMNS, required=False
    ) or _resolve_column(
        source, NIGHTS_TABLE, RESERVATION_CATEGORY_COLUMNS, required=False
    )
    occupancy = (
        _occupancy_expression(source, "reservation_current", "reservation")
        or _occupancy_expression(source, NIGHTS_TABLE, "nights")
    )
    return category_fk, occupancy, True


def build_departure_mix_export(source):
    """Departures per hotel, day, room category and guest count.

    Derived from staging.room_nights_source with exactly the filter and the
    distinct-reservation count that sql/export/arr_dep_data.sql uses for
    total_departures, so this mix sums to that total rather than to something
    close to it. Only the two extra dimensions - room category and guest count -
    come from anywhere else.

    Returns {"export_sql", "prune_sql"}, or None when this mirror cannot answer
    the question - in which case the Cost Data page keeps its blended
    per-departure rate and the flag that explains it.
    """
    end_column = _resolve_column(
        source, NIGHTS_TABLE, NIGHTS_END_COLUMNS, required=False
    )
    hotel_column = _resolve_column(
        source, NIGHTS_TABLE, NIGHTS_HOTEL_COLUMNS, required=False
    )
    reservation_key = _resolve_column(
        source, NIGHTS_TABLE, NIGHTS_RESERVATION_KEY_COLUMNS, required=False
    )
    category_name = _resolve_column(
        source, "resource_category_current", CATEGORY_NAME_COLUMNS, required=False
    )
    category_fk, occupancy, needs_reservation = _departure_dimensions(source)
    if not (end_column and hotel_column and reservation_key and category_name
            and category_fk and occupancy):
        logging.warning(
            "Departure mix unavailable: end=%s hotel=%s reservation_key=%s "
            "category_name=%s category_fk=%s occupancy=%s. Cleaning cost keeps "
            "its flat average per departure.",
            end_column, hotel_column, reservation_key, category_name,
            category_fk, bool(occupancy),
        )
        return None

    departure_date = SQL("(nights.{} {})::date").format(
        Identifier(end_column), STOCKHOLM
    )

    # Where the two extra dimensions are read, which decides both what the
    # collapsing CTE has to carry out of the room nights and whether the join into
    # reservation_current is paid for at all.
    if needs_reservation:
        nights_extra = SQL("")
        reservation_join = SQL(
            "JOIN reservation_current reservation "
            "ON reservation.id::text = departing.reservation_key::text"
        )
        category_reference = SQL("reservation.{}").format(Identifier(category_fk))
        occupancy_reference = occupancy
    else:
        # Both are per-reservation constants, so carrying them through the DISTINCT
        # cannot split a reservation into two rows.
        nights_extra = SQL(
            ", nights.{category_fk} AS category_key, {occupancy} AS occupancy"
        ).format(category_fk=Identifier(category_fk), occupancy=occupancy)
        reservation_join = SQL("")
        category_reference = SQL("departing.category_key")
        occupancy_reference = SQL("departing.occupancy")
    canceled = _resolve_column(
        source, NIGHTS_TABLE, NIGHTS_CANCELED_COLUMNS, required=False
    )

    export_sql = SQL("""
        WITH departing AS (
            -- One row per reservation per date it departs on, before anything is
            -- joined or counted. room_nights_source holds a row per room NIGHT,
            -- so without this the guest-count expression below - which can be a
            -- sum over a PersonCounts list - would be evaluated once per night of
            -- every stay instead of once per departure.
            --
            -- DISTINCT rather than one row per reservation: arr_dep_data.sql
            -- groups on each row's own end_utc date and counts the reservation in
            -- every date group it appears in, so a stay whose rows carry more than
            -- one end_utc is counted on each. Collapsing to max(end_utc) here
            -- would quietly stop this mix summing to that total.
            SELECT DISTINCT
                nights.{reservation_key} AS reservation_key,
                trim(nights.{hotel_column}) AS hotel_name,
                {departure_date} AS stay_date
                {nights_extra}
            FROM {nights_table} nights
            WHERE nights.{end_column} IS NOT NULL
              {canceled}
              AND {departure_date} >= {window}
        )
        SELECT
            md5(concat_ws(
                '|',
                mix.enterprise_id,
                mix.stay_date::text,
                mix.resource_category_id,
                mix.occupancy::text
            )) AS departure_mix_data_key,
            mix.enterprise_id,
            mix.hotel_name,
            mix.stay_date,
            mix.resource_category_id,
            mix.category_name,
            mix.occupancy,
            mix.departures
        FROM (
            SELECT
                enterprise_id,
                hotel_name,
                stay_date,
                resource_category_id,
                category_name,
                occupancy,
                -- One departure per reservation, however many room nights it had.
                count(DISTINCT reservation_key)::int AS departures
            FROM (
                -- Classified before grouped, so the guest count - which for a
                -- PersonCounts list is a scalar subquery - is evaluated once per
                -- departure and not again as a grouping key.
                SELECT
                    enterprise.id::text AS enterprise_id,
                    trim(enterprise.name)::text AS hotel_name,
                    departing.stay_date AS stay_date,
                    departing.reservation_key AS reservation_key,
                    category.id::text AS resource_category_id,
                    trim(category.{category_name})::text AS category_name,
                    {occupancy_reference} AS occupancy
                FROM departing
                JOIN enterprise_current enterprise
                  ON enterprise.tenant_key = 'GCCH'
                 AND trim(enterprise.name) = departing.hotel_name
                {reservation_join}
                JOIN resource_category_current category
                  ON category.id::text = {category_reference}::text
                WHERE nullif(trim(category.{category_name}), '') IS NOT NULL
            ) classified
            GROUP BY 1, 2, 3, 4, 5, 6
        ) mix
        ORDER BY mix.hotel_name, mix.stay_date, mix.category_name, mix.occupancy
    """).format(
        departure_date=departure_date,
        category_name=Identifier(category_name),
        category_reference=category_reference,
        occupancy_reference=occupancy_reference,
        nights_extra=nights_extra,
        reservation_key=Identifier(reservation_key),
        nights_table=table_identifier(NIGHTS_TABLE),
        hotel_column=Identifier(hotel_column),
        reservation_join=reservation_join,
        end_column=Identifier(end_column),
        canceled=(
            SQL("AND nights.{} IS NULL").format(Identifier(canceled))
            if canceled else SQL("")
        ),
        window=Literal(_window_start()),
    )

    return {
        "export_sql": export_sql,
        "prune_sql": _prune_sql("departure_mix_data"),
    }


def build_distribution_mix_export(source):
    """Room revenue per hotel, day, origin, travel agency and rate.

    The weight is the accommodation charge, because that is the revenue a
    distribution fee is charged on. Travel agency and rate are optional: a mirror
    that carries neither still supports the origin level of the rulebook, which
    is the level that decides most of it.

    Returns None when the origin cannot be resolved - the rulebook's top level is
    origin, so without it there is nothing to apportion by.
    """
    origin_column = _resolve_column(
        source, "reservation_current", RESERVATION_ORIGIN_COLUMNS, required=False
    )
    if origin_column is None:
        logging.warning(
            "Distribution mix unavailable: reservation_current carries no origin "
            "column (tried %s). Distribution cost keeps the fallback percentage.",
            list(RESERVATION_ORIGIN_COLUMNS),
        )
        return None

    join = _agency_join(source)
    if join:
        agency_join = SQL(
            "LEFT JOIN {table} agency ON agency.{key}::text = reservation.{fk}::text"
        ).format(
            table=table_identifier(join.table),
            key=Identifier(join.key),
            fk=Identifier(join.fk),
        )
        agency_value = SQL("nullif(trim(agency.{}), '')").format(Identifier(join.name))
    else:
        logging.info(
            "No travel-agency link on reservation_current; the distribution mix "
            "carries origin and rate only, so agency subgroups fall back to their "
            "origin group's percentage"
        )
        agency_join = SQL("")
        agency_value = SQL("NULL::text")

    rate_fk = _resolve_column(
        source, "reservation_current", RESERVATION_RATE_COLUMNS, required=False
    )
    rate_name = _resolve_column(
        source, "rate_current", RATE_NAME_COLUMNS, required=False
    )
    # The mix is matched against rate names an operator saved in Cost Input, so
    # it has to read the same stable name those pickers offered - the current
    # row moves, and a renamed rate would silently stop matching its own rule.
    rate_history = rate_name_lateral(
        lambda table: _table_columns(source, table),
        SQL("reservation.{}").format(Identifier(rate_fk)) if rate_fk else SQL("NULL"),
        alias="named",
        # Left, so a reservation whose rate has no name still carries its origin
        # and agency into the mix instead of dropping out of the weighting.
        outer=True,
    ) if rate_fk else None

    if rate_history:
        rate_join, rate_value = rate_history
    elif rate_fk and rate_name:
        rate_join = SQL(
            "LEFT JOIN rate_current rate ON rate.id::text = reservation.{}::text"
        ).format(Identifier(rate_fk))
        rate_value = SQL("nullif(trim(rate.{}), '')").format(Identifier(rate_name))
    else:
        logging.info(
            "Reservations carry no usable rate link (rate_fk=%s rate_name=%s); the "
            "distribution mix carries origin and agency only",
            rate_fk, rate_name,
        )
        rate_join = SQL("")
        rate_value = SQL("NULL::text")

    item_type = _resolve_column(
        source, "order_item_current", ORDER_ITEM_TYPE_COLUMNS, required=False
    )
    # order_item_current is the accounting-item table: sql/export/total_payment_data.sql
    # reads the same rows as payments. Without the type filter the weight would
    # include deposits and settlements, which are not revenue.
    type_predicate = (
        SQL("AND {} = 'SpaceOrder'").format(
            SQL("item.{}").format(Identifier(item_type))
        )
        if item_type else SQL("")
    )
    if item_type is None:
        logging.info(
            "order_item_current carries no type column (tried %s); the "
            "distribution mix weights by every item charged on the reservation",
            list(ORDER_ITEM_TYPE_COLUMNS),
        )

    stay_date = SQL("(item.start_utc {})::date").format(STOCKHOLM)

    export_sql = SQL("""
        SELECT
            md5(concat_ws(
                '|',
                mix.enterprise_id,
                mix.stay_date::text,
                coalesce(mix.origin, ''),
                coalesce(mix.travel_agency, ''),
                coalesce(mix.rate_name, '')
            )) AS distribution_mix_data_key,
            mix.enterprise_id,
            trim(enterprise.name)::text AS hotel_name,
            mix.stay_date,
            mix.origin,
            mix.travel_agency,
            mix.rate_name,
            mix.room_revenue_net,
            mix.reservation_count
        FROM (
            SELECT
                service.enterprise_id::text AS enterprise_id,
                {stay_date} AS stay_date,
                nullif(trim(reservation.{origin}), '') AS origin,
                {agency_value} AS travel_agency,
                {rate_value} AS rate_name,
                sum(item.amount_net_value) AS room_revenue_net,
                count(DISTINCT item.service_order_id)::int AS reservation_count
            FROM order_item_current item
            JOIN reservation_current reservation
              ON reservation.id = item.service_order_id
            JOIN service_current service
              ON service.id = reservation.service_id
             AND service.name = 'Stay'
            {agency_join}
            {rate_join}
            WHERE item.canceled_utc IS NULL
              AND item.start_utc IS NOT NULL
              {type_predicate}
              {canceled}
              AND {stay_date} >= {window}
            GROUP BY 1, 2, 3, 4, 5
        ) mix
        JOIN enterprise_current enterprise
          ON enterprise.id::text = mix.enterprise_id
         AND enterprise.tenant_key = 'GCCH'
        WHERE nullif(trim(enterprise.name), '') IS NOT NULL
        ORDER BY hotel_name, mix.stay_date
    """).format(
        stay_date=stay_date,
        origin=Identifier(origin_column),
        agency_value=agency_value,
        rate_value=rate_value,
        agency_join=agency_join,
        rate_join=rate_join,
        type_predicate=type_predicate,
        canceled=_canceled_predicate(source),
        window=Literal(_window_start()),
    )

    return {
        "export_sql": export_sql,
        "prune_sql": _prune_sql("distribution_mix_data"),
    }


def _prune_sql(table_name):
    """Remove rows this run did not re-import.

    A mix row is keyed by its dimensions, so a combination that stops occurring
    on a day has no row for the upsert to overwrite and would keep its old
    figure for good. The upsert stamps last_seen_at on every touch; anything
    inside the exported window still carrying an older stamp is gone.

    Bounded to the same window as the export, so history outside it is left
    alone rather than being deleted every night.
    """
    return SQL("""
        DELETE FROM functions.{table}
        WHERE stay_date >= {window}
          AND last_seen_at < %(started_at)s
    """).format(table=Identifier(table_name), window=Literal(_window_start()))


BUILDERS = {
    "departure_mix": build_departure_mix_export,
    "distribution_mix": build_distribution_mix_export,
}


def build_mix_export(name, source):
    """The export plan for one mix, or None when this mirror cannot produce it.

    A missing table raises rather than resolving to None - _resolve_column cannot
    tell "no such column" from "no such table" any other way - and a missing table
    is exactly as good a reason to skip the dataset as a missing column. Both come
    back as None here, so a mirror without resource_category_current leaves the
    page on its previous figure instead of failing the whole cost import.
    """
    builder = BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"Unknown cost mix export '{name}'. Allowed: {sorted(BUILDERS)}")
    try:
        return builder(source)
    except CostSourceUnavailableError as error:
        logging.warning(
            "Cost mix '%s' unavailable, so the dataset is skipped: %s", name, error
        )
        return None
