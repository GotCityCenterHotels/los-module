import logging
import os

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from cost_database import cost_pool
from queries.supplement_source import fetch_pickup_history
from services.supplement_schema_service import ensure_supplement_schema

#s
MAX_GRID_DAYS = 366
STALE_AFTER_HOURS = 36
VALID_LY_COMPARISONS = {"sameDate", "sameWeekday"}
VALID_INVENTORY_BASES = {"sellable", "physical"}
# "summary" skips the source database entirely and answers from the published
# read model, which is the fast half of a detail request.
VALID_DETAIL_INCLUDES = {"all", "summary"}
INVENTORY_EXACT_FROM = date(2026, 2, 27)

_grid_cache = {}
_grid_cache_lock = Lock()
_detail_cache = {}
# The figures-only half, for a dialog that has been opened before the slow half
# of a previous open finished. Once the full payload lands it supersedes this,
# and a summary request is answered by projecting it.
_summary_cache = {}
_detail_cache_lock = Lock()
# The hotel and room-category dimensions change only when a sync publishes, so
# they are held per publication rather than re-read on every grid request that
# misses the payload cache.
_metadata_cache = {}
_metadata_cache_lock = Lock()
# The two fact loads are the expensive part of building a grid, and they do not
# depend on which categories are shown or on which inventory basis is applied -
# both of those are arithmetic over the same rows. Holding them lets a basis
# switch, or the same period reopened, skip straight to assembling cells.
_facts_cache = {}
_facts_cache_lock = Lock()
# Small on purpose. A year of comparison-mode facts is a large map, and this
# exists to make a repeated period cheap, not to hold every period ever asked
# for - the payload cache above already does that, more compactly.
FACTS_CACHE_LIMIT = 4
# Two independent stay dates, so the current and comparison pickup curves are
# rebuilt at the same time instead of one after the other. Each task opens its
# own read-only source connection, which is why the pool is bounded at two:
# these are the slowest queries the app issues interactively.
_pickup_workers = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="supplement-pickup"
)
STOCKHOLM_TIME_ZONE = ZoneInfo(
    os.environ.get("SUPPLEMENT_TIME_ZONE", "Europe/Stockholm")
)


class SupplementUnavailableError(RuntimeError):
    pass


def shift_last_year(value, basis):
    if basis == "sameWeekday":
        return value - timedelta(days=364)
    target_year = value.year - 1
    try:
        return value.replace(year=target_year)
    except ValueError:
        return value.replace(year=target_year, day=28)


def stockholm_today():
    return datetime.now(STOCKHOLM_TIME_ZONE).date()


def add_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))


def validate_date_range(start_date, end_date):
    if start_date > end_date:
        raise ValueError("startDate cannot be after endDate")
    day_count = (end_date - start_date).days + 1
    if day_count > MAX_GRID_DAYS:
        raise ValueError(f"Supplement ranges are limited to {MAX_GRID_DAYS} days")
    return day_count


def calculate_metrics(
    assigned_rooms,
    room_revenue,
    physical_inventory,
    sellable_inventory=None,
    inventory_basis="sellable",
    inventory_quality="exact",
):
    if inventory_basis not in VALID_INVENTORY_BASES:
        raise ValueError("inventoryBasis must be sellable or physical")
    assigned = float(assigned_rooms or 0)
    revenue = float(room_revenue or 0)
    physical = float(physical_inventory or 0)
    sellable = float(
        physical_inventory if sellable_inventory is None else sellable_inventory or 0
    )
    available = sellable if inventory_basis == "sellable" else physical
    return {
        "occ": assigned / available * 100 if available > 0 else None,
        "adr": revenue / assigned if assigned > 0 else None,
        "revpar": revenue / available if available > 0 else None,
        "assignedRooms": assigned,
        "revenue": revenue,
        "inventory": available,
        "physicalInventory": physical,
        "sellableInventory": sellable,
        "inventoryQuality": inventory_quality,
    }


def _empty_fact():
    return {
        "assigned_rooms": Decimal(0),
        "room_revenue": Decimal(0),
        "total_space": Decimal(0),
        "space_to_sell": Decimal(0),
        "inventory_quality": "exact",
    }


def _add_fact(total, fact):
    if fact:
        total["assigned_rooms"] += fact.get("assigned_rooms") or 0
        total["room_revenue"] += fact.get("room_revenue") or 0
        total["total_space"] += fact.get("total_space") or 0
        total["space_to_sell"] += fact.get("space_to_sell") or 0
        if fact.get("inventory_quality") == "approximated-current":
            total["inventory_quality"] = "approximated-current"
    return total


def _metric_fact(fact, inventory_basis):
    fact = fact or _empty_fact()
    return calculate_metrics(
        fact["assigned_rooms"],
        fact["room_revenue"],
        fact["total_space"],
        fact["space_to_sell"],
        inventory_basis,
        fact["inventory_quality"],
    )


def _publication(cursor, required=True):
    cursor.execute("""
        SELECT p.run_id, p.data_as_of, p.published_at,
               c.minimum_stay_date, c.maximum_stay_date,
               c.minimum_snapshot_date, c.maximum_snapshot_date
        FROM functions.supplement_publication p
        LEFT JOIN functions.supplement_coverage c ON c.singleton
        WHERE p.singleton
    """)
    row = cursor.fetchone()
    if row is None and required:
        raise SupplementUnavailableError("Supplement data has not been published yet")
    return row


def _status_payload(publication):
    if publication is None:
        return {
            "status": "unavailable",
            "dataAsOf": None,
            "publishedAt": None,
            "stale": True,
            "coverage": None,
        }
    published_at = publication["published_at"]
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    stale = datetime.now(timezone.utc) - published_at > timedelta(hours=STALE_AFTER_HOURS)
    coverage = None
    if publication.get("minimum_stay_date"):
        coverage = {
            "minimumStayDate": publication["minimum_stay_date"].isoformat(),
            "maximumStayDate": publication["maximum_stay_date"].isoformat(),
            "minimumSnapshotDate": publication["minimum_snapshot_date"].isoformat(),
            "maximumSnapshotDate": publication["maximum_snapshot_date"].isoformat(),
        }
    return {
        "status": "stale" if stale else "available",
        "runId": publication["run_id"],
        "dataAsOf": publication["data_as_of"].isoformat(),
        "publishedAt": published_at.isoformat(),
        "stale": stale,
        "coverage": coverage,
    }


def fetch_supplement_status():
    ensure_supplement_schema()
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            return _status_payload(_publication(cursor, required=False))


def _dimensions(cursor, publication):
    """Hotels and room categories for the current publication.

    Both the metadata route and every grid build need these, and they are two
    small queries whose answer cannot change until a sync publishes again, so
    they are read once per publication instead of once per request.
    """
    run_id = publication["run_id"] if publication else None
    with _metadata_cache_lock:
        cached = _metadata_cache.get(run_id)
    if cached is not None:
        return cached

    cursor.execute("""
        SELECT enterprise_id AS hotel_code, hotel_name
        FROM functions.hotels
        WHERE active
        ORDER BY hotel_name, enterprise_id
    """)
    hotels = [
        {"code": row["hotel_code"], "name": row["hotel_name"]}
        for row in cursor.fetchall()
    ]
    cursor.execute("""
        SELECT hotel_code, room_category_id::text AS room_category_code,
               space_room_name, short_name, sort_order
        FROM functions.supplement_room_categories
        ORDER BY hotel_code, sort_order, space_room_name
    """)
    categories = defaultdict(list)
    for row in cursor.fetchall():
        categories[row["hotel_code"]].append({
            "code": row["room_category_code"],
            "name": row["space_room_name"],
            "shortName": row["short_name"],
            "order": row["sort_order"],
        })

    dimensions = {
        "hotels": hotels,
        # Insertion-ordered, so "the first hotel" means the same thing here as
        # it does in the metadata the browser reads.
        "hotelNames": {hotel["code"]: hotel["name"] for hotel in hotels},
        "categoriesByHotel": dict(categories),
    }
    with _metadata_cache_lock:
        # Exactly one publication is live at a time, so anything held against an
        # older run_id is unreachable.
        _metadata_cache.clear()
        _metadata_cache[run_id] = dimensions
    return dimensions


def list_supplement_hotels():
    ensure_supplement_schema()
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            publication = _publication(cursor, required=False)
            dimensions = _dimensions(cursor, publication)
            return {
                **_status_payload(publication),
                "hotels": dimensions["hotels"],
                "categoriesByHotel": dimensions["categoriesByHotel"],
            }


def _load_latest_facts(cursor, minimum_date, maximum_date, hotel_codes):
    cursor.execute("""
        SELECT i.stay_date, i.hotel_code,
               i.space_room_category_id::text AS room_category_code,
               coalesce(c.assigned_rooms, 0) AS assigned_rooms,
               coalesce(c.room_revenue, 0) AS room_revenue,
               i.total_space, i.space_to_sell, i.inventory_quality
        FROM functions.supplement_latest_inventory i
        LEFT JOIN functions.supplement_latest_category c
          USING (stay_date, hotel_code, space_room_category_id, snapshot_date)
        WHERE i.stay_date BETWEEN %s AND %s
          AND i.hotel_code = ANY(%s)
    """, (minimum_date, maximum_date, hotel_codes))
    return {
        (row["hotel_code"], row["stay_date"], row["room_category_code"]): row
        for row in cursor.fetchall()
    }


def _load_spit_facts(cursor, minimum_date, maximum_date, hotel_codes, as_of_date):
    cursor.execute("""
        WITH chosen_stays AS (
            SELECT stay_date, hotel_code, max(snapshot_date) AS snapshot_date
            FROM functions.supplement_snapshot_inventory
            WHERE stay_date BETWEEN %s AND %s
              AND hotel_code = ANY(%s)
              AND snapshot_date <= %s
            GROUP BY stay_date, hotel_code
        ),
        chosen_inventory AS (
            SELECT i.stay_date, i.hotel_code, i.space_room_category_id,
                   i.snapshot_date, i.total_space, i.space_to_sell,
                   i.inventory_quality
            FROM functions.supplement_snapshot_inventory i
            JOIN chosen_stays s USING (stay_date, hotel_code, snapshot_date)
        )
        SELECT i.stay_date, i.hotel_code,
               i.space_room_category_id::text AS room_category_code,
               coalesce(c.assigned_rooms, 0) AS assigned_rooms,
               coalesce(c.room_revenue, 0) AS room_revenue,
               i.total_space, i.space_to_sell, i.inventory_quality
        FROM chosen_inventory i
        LEFT JOIN functions.supplement_snapshot_category c
          USING (stay_date, hotel_code, space_room_category_id, snapshot_date)
    """, (minimum_date, maximum_date, hotel_codes, as_of_date))
    return {
        (row["hotel_code"], row["stay_date"], row["room_category_code"]): row
        for row in cursor.fetchall()
    }


def _load_facts(
    cursor, run_id, hotel_codes, latest_from, latest_to, ly_start, ly_end, spit_as_of
):
    """The two fact loads behind a grid, held per publication and period.

    Which categories are shown and which inventory basis is applied are both
    arithmetic over these same rows, so switching either used to re-run the two
    heaviest queries in the request for an answer that had not changed.
    """
    key = (run_id, hotel_codes, latest_from, latest_to, ly_start, ly_end, spit_as_of)
    with _facts_cache_lock:
        cached = _facts_cache.get(key)
    if cached is not None:
        logging.info("Supplement fact cache hit run_id=%s", run_id)
        return cached

    facts = (
        _load_latest_facts(cursor, latest_from, latest_to, list(hotel_codes)),
        _load_spit_facts(cursor, ly_start, ly_end, list(hotel_codes), spit_as_of),
    )
    with _facts_cache_lock:
        if len(_facts_cache) >= FACTS_CACHE_LIMIT:
            _facts_cache.clear()
        _facts_cache[key] = facts
    return facts


def _cell_for_categories(source, hotel_code, stay_date, categories):
    total = _empty_fact()
    for category in categories:
        _add_fact(total, source.get((hotel_code, stay_date, category)))
    return total


def _build_dates(start_date, end_date, basis, today):
    dates = []
    current = start_date
    while current <= end_date:
        dates.append({
            "date": current.isoformat(),
            "lyDate": shift_last_year(current, basis).isoformat(),
            "isPast": current < today,
            "isWeekend": current.weekday() in {4, 5},
        })
        current += timedelta(days=1)
    return dates


def _row_cells(hotel_codes, categories_by_hotel, dates, latest, spit, inventory_basis):
    cells = []
    for date_info in dates:
        stay_date = date.fromisoformat(date_info["date"])
        ly_date = date.fromisoformat(date_info["lyDate"])
        facts = {"today": _empty_fact(), "ly": _empty_fact(), "spit": _empty_fact()}
        for hotel_code in hotel_codes:
            categories = categories_by_hotel.get(hotel_code, [])
            _add_fact(facts["today"], _cell_for_categories(latest, hotel_code, stay_date, categories))
            _add_fact(facts["ly"], _cell_for_categories(latest, hotel_code, ly_date, categories))
            _add_fact(facts["spit"], _cell_for_categories(spit, hotel_code, ly_date, categories))
        cells.append({
            key: _metric_fact(value, inventory_basis) for key, value in facts.items()
        })
    return cells


def fetch_supplement_grid(
    start_date,
    end_date,
    mode="single",
    hotel_codes=None,
    room_categories=None,
    ly_comparison_basis="sameDate",
    inventory_basis="sellable",
):
    validate_date_range(start_date, end_date)
    today = stockholm_today()
    minimum_allowed = add_months(today, -36)
    maximum_allowed = add_months(today, 18)
    if start_date < minimum_allowed or end_date > maximum_allowed:
        raise ValueError(
            f"Supplement stay dates must be between {minimum_allowed} and {maximum_allowed}"
        )
    if mode not in {"single", "comparison"}:
        raise ValueError("mode must be single or comparison")
    if ly_comparison_basis not in VALID_LY_COMPARISONS:
        raise ValueError("lyComparisonBasis must be sameDate or sameWeekday")
    if inventory_basis not in VALID_INVENTORY_BASES:
        raise ValueError("inventoryBasis must be sellable or physical")
    requested_hotels = tuple(sorted(set(hotel_codes or ())))
    requested_categories = tuple(sorted(set(room_categories or ())))
    ensure_supplement_schema()

    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            publication = _publication(cursor)
            status = _status_payload(publication)
            coverage = status.get("coverage")
            if coverage and (
                start_date < date.fromisoformat(coverage["minimumStayDate"])
                or end_date > date.fromisoformat(coverage["maximumStayDate"])
            ):
                raise SupplementUnavailableError(
                    "The selected dates have not been backfilled into PostgreSQL"
                )
            cache_key = (
                publication["run_id"], status["status"], start_date, end_date, mode,
                requested_hotels, requested_categories,
                ly_comparison_basis, inventory_basis,
            )
            with _grid_cache_lock:
                cached = _grid_cache.get(cache_key)
            if cached is not None:
                logging.info("Supplement grid cache hit run_id=%s", publication["run_id"])
                return cached
            logging.info("Supplement grid cache miss run_id=%s", publication["run_id"])
            dimensions = _dimensions(cursor, publication)
            hotel_lookup = dimensions["hotelNames"]
            requested_hotel_set = set(requested_hotels)
            selected_hotels = [code for code in hotel_lookup if code in requested_hotel_set]
            if not selected_hotels and hotel_lookup:
                selected_hotels = [next(iter(hotel_lookup))] if mode == "single" else list(hotel_lookup)
            if not selected_hotels:
                raise SupplementUnavailableError("No Supplement hotels are available")
            if mode == "single":
                selected_hotels = selected_hotels[:1]

            category_metadata = defaultdict(list)
            short_names = {}
            category_names = {}
            for hotel_code in selected_hotels:
                for category in dimensions["categoriesByHotel"].get(hotel_code, ()):
                    code = category["code"]
                    category_metadata[hotel_code].append(code)
                    short_names[(hotel_code, code)] = category["shortName"]
                    category_names[(hotel_code, code)] = category["name"]
            if mode == "single" and requested_categories:
                allowed = set(requested_categories)
                category_metadata[selected_hotels[0]] = [
                    item for item in category_metadata[selected_hotels[0]] if item in allowed
                ]

            ly_start = shift_last_year(start_date, ly_comparison_basis)
            ly_end = shift_last_year(end_date, ly_comparison_basis)
            spit_as_of = shift_last_year(publication["data_as_of"], ly_comparison_basis)
            latest, spit = _load_facts(
                cursor,
                publication["run_id"],
                tuple(selected_hotels),
                min(start_date, ly_start),
                max(end_date, ly_end),
                ly_start,
                ly_end,
                spit_as_of,
            )
            dates = _build_dates(start_date, end_date, ly_comparison_basis, today)

            rows = []
            if mode == "single":
                hotel_code = selected_hotels[0]
                for category in category_metadata[hotel_code]:
                    rows.append({
                        "rowType": "category",
                        "code": category,
                        "label": category_names.get((hotel_code, category), category),
                        "shortLabel": short_names.get((hotel_code, category), category[:8].upper()),
                        "isTotal": False,
                        "cells": _row_cells(
                            [hotel_code], {hotel_code: [category]}, dates, latest, spit,
                            inventory_basis,
                        ),
                    })
                if category_metadata[hotel_code]:
                    rows.append({
                        "rowType": "category",
                        "code": "total",
                        "label": "Selected categories",
                        "shortLabel": "Total",
                        "isTotal": True,
                        "cells": _row_cells(
                            [hotel_code], category_metadata, dates, latest, spit,
                            inventory_basis,
                        ),
                    })
            else:
                for hotel_code in selected_hotels:
                    rows.append({
                        "rowType": "hotel",
                        "code": hotel_code,
                        "label": hotel_lookup[hotel_code],
                        "shortLabel": hotel_lookup[hotel_code],
                        "isTotal": False,
                        "cells": _row_cells(
                            [hotel_code], category_metadata, dates, latest, spit,
                            inventory_basis,
                        ),
                    })
                rows.append({
                    "rowType": "hotel",
                    "code": "total",
                    "label": "Selected hotels",
                    "shortLabel": "Total",
                    "isTotal": True,
                    "cells": _row_cells(
                        selected_hotels, category_metadata, dates, latest, spit,
                        inventory_basis,
                    ),
                })

            payload = {
                **status,
                "parameters": {
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "mode": mode,
                    "hotelCodes": selected_hotels,
                    "lyComparisonBasis": ly_comparison_basis,
                    "inventoryBasis": inventory_basis,
                },
                "inventoryBasis": inventory_basis,
                "inventoryQuality": (
                    "approximated-current"
                    if any(
                        cell[mode]["inventoryQuality"] == "approximated-current"
                        for row in rows for cell in row["cells"]
                        for mode in ("today", "ly", "spit")
                    ) else "exact"
                ),
                "inventoryExactFrom": INVENTORY_EXACT_FROM.isoformat(),
                "spitMethod": "lifecycle",
                "dates": dates,
                "rows": rows,
                "totals": next((row for row in reversed(rows) if row["isTotal"]), None),
            }
            with _grid_cache_lock:
                if len(_grid_cache) >= 128:
                    _grid_cache.clear()
                _grid_cache[cache_key] = payload
            return payload


def _detail_rows(cursor, table, hotel_code, stay_date, category, as_of=None):
    category_clause = "AND space_room_category_id = %(category)s::uuid" if category else ""
    if table == "latest":
        cursor.execute(f"""
            SELECT requested_room_category_id::text AS requested_room_category_code,
                   max(requested_room_name) AS requested_room_name,
                   sum(assigned_rooms) AS assigned_rooms,
                   sum(room_revenue) AS room_revenue
            FROM functions.supplement_latest_detail
            WHERE hotel_code = %(hotel_code)s AND stay_date = %(stay_date)s
              {category_clause}
            GROUP BY requested_room_category_id
        """, {"hotel_code": hotel_code, "stay_date": stay_date, "category": category})
    else:
        detail_category_clause = (
            "AND d.space_room_category_id = %(category)s::uuid" if category else ""
        )
        cursor.execute(f"""
            WITH chosen AS (
                SELECT max(snapshot_date) AS snapshot_date
                FROM functions.supplement_snapshot_inventory
                WHERE hotel_code = %(hotel_code)s AND stay_date = %(stay_date)s
                  AND snapshot_date <= %(as_of)s
            )
            SELECT d.requested_room_category_id::text AS requested_room_category_code,
                   max(d.requested_room_name) AS requested_room_name,
                   sum(d.assigned_rooms) AS assigned_rooms,
                   sum(d.room_revenue) AS room_revenue
            FROM functions.supplement_snapshot_detail d, chosen c
            WHERE d.hotel_code = %(hotel_code)s AND d.stay_date = %(stay_date)s
              AND d.snapshot_date = c.snapshot_date {detail_category_clause}
            GROUP BY d.requested_room_category_id
        """, {
            "hotel_code": hotel_code, "stay_date": stay_date,
            "category": category, "as_of": as_of,
        })
    return cursor.fetchall()


def _stored_inventory_for_dates(cursor, hotel_code, windows, category):
    """Per-snapshot inventory for the days a sync actually materialised.

    windows is {stay_date: maximum_snapshot_date}. The current and comparison
    curves want the same shape of answer for two dates with two different
    cutoffs, and asking twice meant two round trips for one index's worth of
    work - the lookup index leads on (hotel_code, stay_date, snapshot_date), so
    the two branches are a bitmap OR over it.

    Deliberately has no lower bound: the old "snapshot_date BETWEEN stay_date -
    366 AND stay_date + 7" clipped the curve at a year regardless of what was
    asked for.
    """
    category_clause = (
        "AND i.space_room_category_id = %(category)s::uuid" if category else ""
    )
    branches = " OR ".join(
        f"(i.stay_date = %(stay_date_{index})s"
        f" AND i.snapshot_date <= %(cutoff_{index})s)"
        for index in range(len(windows))
    )
    parameters = {"hotel_code": hotel_code, "category": category}
    for index, (stay_date, cutoff) in enumerate(windows.items()):
        parameters[f"stay_date_{index}"] = stay_date
        parameters[f"cutoff_{index}"] = cutoff

    cursor.execute(f"""
        SELECT i.stay_date, i.snapshot_date,
               sum(i.total_space) AS total_space,
               sum(i.space_to_sell) AS space_to_sell,
               CASE WHEN bool_or(i.inventory_quality = 'approximated-current')
                    THEN 'approximated-current' ELSE 'exact' END AS inventory_quality
        FROM functions.supplement_snapshot_inventory i
        WHERE i.hotel_code = %(hotel_code)s
          AND ({branches})
          {category_clause}
        GROUP BY i.stay_date, i.snapshot_date
    """, parameters)

    by_stay_date = {stay_date: {} for stay_date in windows}
    for row in cursor.fetchall():
        by_stay_date.setdefault(row["stay_date"], {})[row["snapshot_date"]] = row
    return by_stay_date


def _latest_inventory_for_dates(cursor, hotel_code, stay_dates, category):
    """Latest known inventory for several stay dates, in one round trip.

    This answers three former queries at once: the summary figure for the stay
    date, and the fallback each of the two pickup curves uses for days no sync
    materialised. They were three separate reads of the same table, two of them
    for identical arguments.
    """
    # Built conditionally rather than binding a NULL the planner cannot type.
    inventory_category_clause = (
        "AND space_room_category_id = %(category)s::uuid" if category else ""
    )
    cursor.execute(f"""
        SELECT stay_date,
               sum(total_space) AS total_space,
               sum(space_to_sell) AS space_to_sell,
               CASE WHEN bool_or(inventory_quality = 'approximated-current')
                    THEN 'approximated-current' ELSE 'exact' END AS inventory_quality
        FROM functions.supplement_latest_inventory
        WHERE hotel_code = %(hotel_code)s
          AND stay_date = ANY(%(stay_dates)s)
          {inventory_category_clause}
        GROUP BY stay_date
    """, {
        "hotel_code": hotel_code,
        "stay_dates": list(stay_dates),
        "category": category,
    })
    found = {row["stay_date"]: row for row in cursor.fetchall()}
    # A stay date with no inventory rows drops out of a GROUP BY, where the old
    # ungrouped aggregate returned one all-null row. An empty mapping reads the
    # same downstream, because every consumer goes through .get(...) or 0.
    return {stay_date: found.get(stay_date, {}) for stay_date in stay_dates}


def _pickup_rows(history, stored, fallback):
    """Full pickup curve for one stay date, back to the first booking.

    Rooms and revenue are rebuilt from reservation lifecycle in integration_db,
    so the curve reaches as far back as bookings exist rather than as far back as
    the snapshot pipeline happens to have run. Inventory still comes from stored
    snapshots where a sync materialised them; days without one fall back to the
    latest known inventory and are flagged approximated-current, matching how
    pre-2026-02-27 inventory is already reported.
    """
    if not history:
        return []

    rows = []
    for point in history:
        snapshot_date = point["snapshot_date"]
        inventory = stored.get(snapshot_date)
        if inventory is None:
            inventory = {
                "total_space": fallback.get("total_space"),
                "space_to_sell": fallback.get("space_to_sell"),
                "inventory_quality": "approximated-current",
            }
        rows.append({
            "snapshot_date": snapshot_date,
            "assigned_rooms": point["assigned_rooms"],
            "room_revenue": point["room_revenue"],
            "total_space": inventory["total_space"],
            "space_to_sell": inventory["space_to_sell"],
            "inventory_quality": inventory["inventory_quality"],
        })
    return rows


def _slice_pickup(points, days_before_stay):
    """Keep the requested lookback window. None means the whole history.

    Slicing happens here rather than in SQL so the cached series is always the
    full curve: changing the window never re-queries the source, and there is no
    ceiling in the query path that could silently clip a large request.
    """
    if days_before_stay is None:
        return points
    return [
        point for point in points
        if point["daysBeforeStay"] <= days_before_stay
    ]


def _windowed_payload(payload, days_before_stay):
    """A view of the cached full-history payload for one lookback window."""
    pickup = _slice_pickup(payload["pickup"], days_before_stay)
    comparison = _slice_pickup(payload["comparisonPickup"], days_before_stay)
    available = payload.get("pickupHistoryDays")
    return {
        **payload,
        "pickup": pickup,
        "comparisonPickup": comparison,
        # What the client asked for, and what actually exists, so the control can
        # show the true ceiling instead of pretending an empty tail is data.
        "daysBeforeStay": days_before_stay,
        "pickupHistoryDays": available,
    }


PICKUP_FIELDS = ("pickup", "comparisonPickup", "pickupHistoryDays", "daysBeforeStay")


def _summary_view(payload):
    """The figures alone, with the curves stripped out.

    Everything the dialog puts above the chart - rooms, rate, inventory,
    occupancy, booking mix - comes from the published read model in Database A
    and is ready in milliseconds. The curves are rebuilt from reservation
    lifecycle in the source database and are the slow half. Serving the figures
    on their own lets the dialog fill in as soon as they land instead of holding
    an empty panel until the slow half finishes.
    """
    return {key: value for key, value in payload.items() if key not in PICKUP_FIELDS}


def fetch_supplement_detail(
    hotel_code,
    stay_date,
    category,
    ly_comparison_basis,
    inventory_basis="sellable",
    days_before_stay=None,
    include="all",
):
    if ly_comparison_basis not in VALID_LY_COMPARISONS:
        raise ValueError("lyComparisonBasis must be sameDate or sameWeekday")
    if inventory_basis not in VALID_INVENTORY_BASES:
        raise ValueError("inventoryBasis must be sellable or physical")
    if include not in VALID_DETAIL_INCLUDES:
        raise ValueError("include must be all or summary")
    if days_before_stay is not None and days_before_stay < 1:
        raise ValueError("daysBeforeStay must be at least 1")
    if category:
        # The category used to be checked by a query that cast it to uuid, which
        # accepted any spelling Postgres recognises. Normalising to the canonical
        # form up front keeps that tolerance now the check is made in memory, and
        # keeps two spellings of one category off two cache keys.
        try:
            category = str(UUID(str(category)))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("Unknown Supplement room category") from None
    today = stockholm_today()
    minimum_allowed = add_months(today, -36)
    maximum_allowed = add_months(today, 18)
    if stay_date < minimum_allowed or stay_date > maximum_allowed:
        raise ValueError(
            f"Supplement stay dates must be between {minimum_allowed} and {maximum_allowed}"
        )
    ensure_supplement_schema()
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            publication = _publication(cursor)
            status = _status_payload(publication)
            cache_key = (
                publication["run_id"], status["status"], hotel_code, stay_date, category,
                ly_comparison_basis, inventory_basis,
            )
            with _detail_cache_lock:
                cached = _detail_cache.get(cache_key)
                cached_summary = _summary_cache.get(cache_key)
            if cached is not None:
                logging.info("Supplement detail cache hit run_id=%s", publication["run_id"])
                # The cache holds the complete curve, so changing the lookback
                # window is a slice of memory rather than another source query -
                # and a request for the figures alone is a projection of it.
                if include == "summary":
                    return _summary_view(cached)
                return _windowed_payload(cached, days_before_stay)
            if include == "summary" and cached_summary is not None:
                logging.info(
                    "Supplement detail summary cache hit run_id=%s",
                    publication["run_id"],
                )
                return cached_summary
            logging.info("Supplement detail cache miss run_id=%s", publication["run_id"])
            comparison_date = shift_last_year(stay_date, ly_comparison_basis)
            coverage = status.get("coverage")
            if coverage and (
                stay_date < date.fromisoformat(coverage["minimumStayDate"])
                or stay_date > date.fromisoformat(coverage["maximumStayDate"])
            ):
                raise SupplementUnavailableError(
                    "The selected detail date has not been backfilled into PostgreSQL"
                )
            # Answered from the dimensions already held for this publication
            # rather than from two more round trips. Same rules: an active hotel,
            # and a category that belongs to it.
            dimensions = _dimensions(cursor, publication)
            if hotel_code not in dimensions["hotelNames"]:
                raise ValueError("Unknown Supplement hotel")
            if category and not any(
                item["code"] == category
                for item in dimensions["categoriesByHotel"].get(hotel_code, ())
            ):
                raise ValueError("Unknown Supplement room category")
            future = stay_date >= today
            comparison_as_of = shift_last_year(publication["data_as_of"], ly_comparison_basis)
            comparison_pickup_cutoff = (
                comparison_as_of if future else comparison_date + timedelta(days=7)
            )

            # The two lifecycle rebuilds are the slowest thing this endpoint
            # does, they are independent of each other, and they are independent
            # of every Database A query below. Starting them here means the
            # dialog waits for the slower of the two rather than for both in
            # turn, with all the read-model work overlapped behind them. Started
            # only after the hotel and category have been validated, so an
            # unknown identifier still costs nothing at the source - and not at
            # all when only the figures were asked for.
            current_history = None
            comparison_history = None
            if include != "summary":
                current_history = _pickup_workers.submit(
                    fetch_pickup_history,
                    hotel_code, stay_date, category, publication["data_as_of"],
                )
                comparison_history = _pickup_workers.submit(
                    fetch_pickup_history,
                    hotel_code, comparison_date, category, comparison_pickup_cutoff,
                )

            current_rows = _detail_rows(cursor, "latest", hotel_code, stay_date, category)
            comparison_rows = _detail_rows(
                cursor,
                "snapshot" if future else "latest",
                hotel_code,
                comparison_date,
                category,
                comparison_as_of,
            )
            comparison_map = {
                row["requested_room_category_code"]: row for row in comparison_rows
            }
            requested_codes = sorted({
                row["requested_room_category_code"] for row in current_rows
            } | set(comparison_map))
            current_map = {
                row["requested_room_category_code"]: row for row in current_rows
            }
            breakdown = []
            for code in requested_codes:
                current = current_map.get(code) or {}
                comparison = comparison_map.get(code) or {}
                current_rooms = float(current.get("assigned_rooms") or 0)
                comparison_rooms = float(comparison.get("assigned_rooms") or 0)
                breakdown.append({
                    "requestedRoomCategoryId": code,
                    "requestedRoomName": (
                        current.get("requested_room_name")
                        or comparison.get("requested_room_name")
                        or code
                    ),
                    "assignedRooms": current_rooms,
                    "averagePrice": float(current.get("room_revenue") or 0) / current_rooms if current_rooms else None,
                    "comparisonAssignedRooms": comparison_rooms,
                    "comparisonAveragePrice": float(comparison.get("room_revenue") or 0) / comparison_rooms if comparison_rooms else None,
                })

            # Both curves' inventory comes from the read model over the
            # connection already in hand, so it is gathered while the source
            # rebuilds are still running. Two reads cover what used to be five:
            # the summary figure and the two curves' fallbacks all came from the
            # same table, twice with identical arguments.
            latest_inventory = _latest_inventory_for_dates(
                cursor, hotel_code, (stay_date, comparison_date), category
            )
            inventory = latest_inventory[stay_date]
            total_assigned = sum(float(row.get("assigned_rooms") or 0) for row in current_rows)
            total_revenue = sum(float(row.get("room_revenue") or 0) for row in current_rows)
            summary = {
                **status,
                "hotelCode": hotel_code,
                "stayDate": stay_date.isoformat(),
                "roomCategory": category,
                "comparison": "SPIT" if future else "LY",
                "comparisonStayDate": comparison_date.isoformat(),
                "totalAssignedRooms": total_assigned,
                "totalAveragePrice": total_revenue / total_assigned if total_assigned else None,
                "totalSpace": float(inventory.get("total_space") or 0),
                "spaceToSell": float(inventory.get("space_to_sell") or 0),
                "physicalInventory": float(inventory.get("total_space") or 0),
                "sellableInventory": float(inventory.get("space_to_sell") or 0),
                "inventoryBasis": inventory_basis,
                "inventory": float(
                    inventory.get(
                        "space_to_sell" if inventory_basis == "sellable" else "total_space"
                    ) or 0
                ),
                "inventoryExactFrom": INVENTORY_EXACT_FROM.isoformat(),
                "spitMethod": "lifecycle",
                "breakdown": breakdown,
            }

            if include == "summary":
                # No curve was fetched, so the two fields the curve can influence
                # are reported from what this half actually knows. The full
                # request that follows carries the settled values.
                summary_payload = {
                    **summary,
                    "comparisonAvailable": bool(comparison_rows),
                    "inventoryQuality": (
                        "approximated-current"
                        if inventory.get("inventory_quality") == "approximated-current"
                        else "exact"
                    ),
                }
                with _detail_cache_lock:
                    if len(_summary_cache) >= 256:
                        _summary_cache.clear()
                    _summary_cache[cache_key] = summary_payload
                return summary_payload

            stored_inventory = _stored_inventory_for_dates(
                cursor,
                hotel_code,
                {
                    stay_date: publication["data_as_of"],
                    comparison_date: comparison_pickup_cutoff,
                },
                category,
            )
            current_stored = stored_inventory[stay_date]
            current_fallback = inventory
            comparison_stored = stored_inventory[comparison_date]
            comparison_fallback = latest_inventory[comparison_date]

            # The curves are rebuilt from the source database, over the network,
            # under a statement ceiling deliberately tighter than the proxy's.
            # They are also the only part of this response that can fail that
            # way - the figures beside them are already in hand, from the
            # published read model. Failing the whole request over the curves
            # threw those away too and left the dialog with nothing but "Unable
            # to retrieve Supplement detail". They degrade instead: the figures
            # go out, and the curve says why it is missing.
            pickup_error = None
            try:
                current_rebuild = current_history.result()
                comparison_rebuild = comparison_history.result()
            except Exception as error:
                pickup_error = error
                current_rebuild = []
                comparison_rebuild = []
                logging.exception(
                    "Supplement pickup rebuild failed hotel_code=%s stay_date=%s "
                    "sqlstate=%s",
                    hotel_code,
                    stay_date,
                    getattr(error, "sqlstate", None) or "none",
                )

            pickup = []
            for row in _pickup_rows(
                current_rebuild, current_stored, current_fallback
            ):
                rooms = float(row["assigned_rooms"] or 0)
                pickup.append({
                    "viewDate": row["snapshot_date"].isoformat(),
                    "daysBeforeStay": (stay_date - row["snapshot_date"]).days,
                    "assignedRooms": rooms,
                    "averagePrice": float(row["room_revenue"] or 0) / rooms if rooms else None,
                    "totalSpace": float(row["total_space"] or 0),
                    "spaceToSell": float(row["space_to_sell"] or 0),
                    "physicalInventory": float(row["total_space"] or 0),
                    "sellableInventory": float(row["space_to_sell"] or 0),
                    "inventoryQuality": row["inventory_quality"],
                })
            comparison_pickup = []
            for row in _pickup_rows(
                comparison_rebuild, comparison_stored, comparison_fallback
            ):
                rooms = float(row["assigned_rooms"] or 0)
                comparison_pickup.append({
                    "viewDate": row["snapshot_date"].isoformat(),
                    "daysBeforeStay": (comparison_date - row["snapshot_date"]).days,
                    "assignedRooms": rooms,
                    "averagePrice": float(row["room_revenue"] or 0) / rooms if rooms else None,
                    "physicalInventory": float(row["total_space"] or 0),
                    "sellableInventory": float(row["space_to_sell"] or 0),
                    "inventoryQuality": row["inventory_quality"],
                })
            inventory_quality = (
                "approximated-current"
                if inventory.get("inventory_quality") == "approximated-current"
                or any(
                    point["inventoryQuality"] == "approximated-current"
                    for point in pickup + comparison_pickup
                )
                else "exact"
            )
            payload = {
                **summary,
                # The two fields the curves feed into, now that they exist.
                "inventoryQuality": inventory_quality,
                "comparisonAvailable": bool(comparison_rows or comparison_pickup),
                "pickup": pickup,
                "comparisonPickup": comparison_pickup,
                # How far the reconstructed history actually reaches, so the
                # lookback control can bound itself to real data.
                "pickupHistoryDays": (
                    max(point["daysBeforeStay"] for point in pickup)
                    if pickup else 0
                ),
                # An empty curve because there is no history reads the same as an
                # empty curve because the rebuild failed, and they are not the
                # same thing. The reader is told which.
                "pickupAvailable": pickup_error is None,
            }
            if pickup_error is not None:
                payload["pickupUnavailableReason"] = (
                    "The pickup history could not be rebuilt from the source "
                    "database. The figures above are published data and are "
                    "unaffected."
                )
                # Deliberately not cached. A transient source failure held for
                # the cache's lifetime would turn one bad minute into a curve
                # that stays missing until the next publication.
                return _windowed_payload(payload, days_before_stay)
            with _detail_cache_lock:
                if len(_detail_cache) >= 256:
                    _detail_cache.clear()
                # Cache the complete curve; window slicing happens on the way out.
                _detail_cache[cache_key] = payload
            return _windowed_payload(payload, days_before_stay)
