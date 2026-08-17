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
    table_identifier,
)


# Reservation-level exports are the two most expensive statements in the cost
# import, so they are bounded rather than reading all history like the five
# hotel-per-day exports do. The window matches COST_SOURCE_WINDOW_DAYS, so the
# mix covers exactly the period the Cost Input pickers offer values from; a
# period older than this falls back to the previous behaviour and says so.
MIX_WINDOW_DAYS = int(os.environ.get("COST_MIX_WINDOW_DAYS", "730"))

# Mews Reservation.EndUtc - the departure. In order of likelihood.
RESERVATION_END_COLUMNS = (
    "end_utc", "departure_utc", "actual_end_utc", "end_time_utc",
)
# Mews Reservation.RequestedResourceCategoryId / AssignedResourceCategoryId. The
# cleaning rulebook is written per room category, so this is what binds a
# departure to a configured row.
RESERVATION_CATEGORY_COLUMNS = (
    "requested_resource_category_id", "requested_category_id",
    "assigned_resource_category_id", "resource_category_id", "category_id",
)
# Mews Reservation.AdultCount and .ChildCount. Kept as two separate lists, and
# the single-total candidates below are only consulted when neither is present:
# adding a "person count" that already includes children to a child count would
# double the occupancy and cost every family room at the wrong row.
RESERVATION_ADULT_COLUMNS = ("adult_count", "adults", "number_of_adults")
RESERVATION_CHILD_COLUMNS = ("child_count", "children", "number_of_children")
RESERVATION_PERSON_COLUMNS = (
    "person_count", "guest_count", "total_person_count", "persons",
)
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


def _occupancy_expression(source):
    """Guests in the room, however this mirror records them.

    Floored at one: a reservation with no counts recorded is still a room that
    was turned over, and costing it at zero minutes would understate the day.
    """
    adult = _resolve_column(
        source, "reservation_current", RESERVATION_ADULT_COLUMNS, required=False
    )
    child = _resolve_column(
        source, "reservation_current", RESERVATION_CHILD_COLUMNS, required=False
    )
    if adult:
        parts = SQL("coalesce(reservation.{}, 0)").format(Identifier(adult))
        if child:
            parts = SQL("{} + coalesce(reservation.{}, 0)").format(
                parts, Identifier(child)
            )
        return SQL("greatest(1, ({})::int)").format(parts)

    person = _resolve_column(
        source, "reservation_current", RESERVATION_PERSON_COLUMNS, required=False
    )
    if person:
        return SQL("greatest(1, coalesce(reservation.{}, 0)::int)").format(
            Identifier(person)
        )
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


def build_departure_mix_export(source):
    """Departures per hotel, day, room category and guest count.

    Returns {"export_sql", "prune_sql"}, or None when this mirror cannot answer
    the question - in which case the Cost Data page keeps its blended per-departure
    rate and the flag that explains it.
    """
    end_column = _resolve_column(
        source, "reservation_current", RESERVATION_END_COLUMNS, required=False
    )
    category_fk = _resolve_column(
        source, "reservation_current", RESERVATION_CATEGORY_COLUMNS, required=False
    )
    category_name = _resolve_column(
        source, "resource_category_current", CATEGORY_NAME_COLUMNS, required=False
    )
    occupancy = _occupancy_expression(source)
    if not (end_column and category_fk and category_name and occupancy):
        logging.warning(
            "Departure mix unavailable: end=%s category_fk=%s category_name=%s "
            "occupancy=%s. Cleaning cost keeps its blended per-departure rate.",
            end_column, category_fk, category_name, bool(occupancy),
        )
        return None

    window = Literal(_window_start())
    departure_date = SQL("(reservation.{} {})::date").format(
        Identifier(end_column), STOCKHOLM
    )

    export_sql = SQL("""
        SELECT
            md5(concat_ws(
                '|',
                mix.enterprise_id,
                mix.stay_date::text,
                mix.resource_category_id,
                mix.occupancy::text
            )) AS departure_mix_data_key,
            mix.enterprise_id,
            trim(enterprise.name)::text AS hotel_name,
            mix.stay_date,
            mix.resource_category_id,
            mix.category_name,
            mix.occupancy,
            mix.departures
        FROM (
            SELECT
                service.enterprise_id::text AS enterprise_id,
                {departure_date} AS stay_date,
                category.id::text AS resource_category_id,
                trim(category.{category_name})::text AS category_name,
                {occupancy} AS occupancy,
                count(*)::int AS departures
            FROM reservation_current reservation
            JOIN service_current service
              ON service.id = reservation.service_id
             AND service.name = 'Stay'
            JOIN resource_category_current category
              ON category.id::text = reservation.{category_fk}::text
            WHERE reservation.{end_column} IS NOT NULL
              {canceled}
              AND {departure_date} >= {window}
              AND nullif(trim(category.{category_name}), '') IS NOT NULL
            GROUP BY 1, 2, 3, 4, 5
        ) mix
        JOIN enterprise_current enterprise
          ON enterprise.id::text = mix.enterprise_id
         AND enterprise.tenant_key = 'GCCH'
        WHERE nullif(trim(enterprise.name), '') IS NOT NULL
        ORDER BY hotel_name, mix.stay_date, mix.category_name, mix.occupancy
    """).format(
        departure_date=departure_date,
        category_name=Identifier(category_name),
        category_fk=Identifier(category_fk),
        end_column=Identifier(end_column),
        occupancy=occupancy,
        canceled=_canceled_predicate(source),
        window=window,
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
    if rate_fk and rate_name:
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
