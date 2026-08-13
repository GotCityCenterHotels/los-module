import argparse
import json

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from psycopg.rows import dict_row

from queries.supplement_source import (
    iter_booking_lifecycle_batches,
    iter_inventory_batches,
)
from services.supplement_sync_service import add_months
from shared.db import get_import_connection


def _source_facts(snapshot_date, hotel_code, stay_date, category):
    requested = defaultdict(lambda: [Decimal(0), Decimal(0)])
    inventory = {}
    reservations = defaultdict(lambda: [Decimal(0), Decimal(0), None])
    for batch in iter_booking_lifecycle_batches(
        [snapshot_date], stay_date, stay_date,
    ):
        for row in batch:
            if str(row["enterprise_id"]) != hotel_code or row["stay_date"] != stay_date:
                continue
            if row["reservation_created_date"] > snapshot_date:
                continue
            if row["reservation_cancelled_date"] and row["reservation_cancelled_date"] <= snapshot_date:
                continue
            if row["item_created_date"] > snapshot_date:
                continue
            if row["item_cancelled_date"] and row["item_cancelled_date"] <= snapshot_date:
                continue
            if category and str(row["space_category_id"]) != category:
                continue
            key = (
                str(row["reservation_id"]), str(row["space_category_id"]),
                str(row["requested_category_id"]),
            )
            reservations[key][0] = Decimal(1)
            reservations[key][1] += row["gross_revenue"] or 0
            reservations[key][2] = row["requested_category_name"].strip()
    for (_reservation_id, _space_category_id, requested_id), values in reservations.items():
        requested[requested_id][0] += values[0]
        requested[requested_id][1] += values[1]
    for batch in iter_inventory_batches([snapshot_date]):
        for row in batch:
            if str(row["enterprise_id"]) != hotel_code:
                continue
            category_id = str(row["category_id"])
            if category and category_id != category:
                continue
            inventory[category_id] = [
                row["physical_inventory"], row["sellable_inventory"],
                row["inventory_quality"],
            ]
    return requested, inventory


def _application_facts(snapshot_date, hotel_code, stay_date, category):
    category_filter = (
        "AND space_room_category_id = %(category)s::uuid" if category else ""
    )
    parameters = {
        "snapshot_date": snapshot_date,
        "hotel_code": hotel_code,
        "stay_date": stay_date,
        "category": category,
    }
    with get_import_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(f"""
                SELECT requested_room_category_id::text AS requested_room_category_id,
                       assigned_rooms, room_revenue
                FROM functions.supplement_snapshot_detail
                WHERE snapshot_date = %(snapshot_date)s
                  AND hotel_code = %(hotel_code)s
                  AND stay_date = %(stay_date)s
                  {category_filter}
            """, parameters)
            requested = {
                row["requested_room_category_id"]: [
                    row["assigned_rooms"], row["room_revenue"]
                ]
                for row in cursor.fetchall()
            }
            cursor.execute(f"""
                SELECT space_room_category_id::text AS space_room_category_id,
                       total_space, space_to_sell, inventory_quality
                FROM functions.supplement_snapshot_inventory
                WHERE snapshot_date = %(snapshot_date)s
                  AND hotel_code = %(hotel_code)s
                  AND stay_date = %(stay_date)s
                  {category_filter}
            """, parameters)
            inventory = {
                row["space_room_category_id"]: [
                    row["total_space"], row["space_to_sell"], row["inventory_quality"]
                ]
                for row in cursor.fetchall()
            }
    return requested, inventory


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Compare one representative bounded Database B slice with Database A."
    )
    parser.add_argument("snapshot_date", type=date.fromisoformat)
    parser.add_argument("hotel_code")
    parser.add_argument("stay_date", type=date.fromisoformat)
    parser.add_argument("--category")
    arguments = parser.parse_args()
    if arguments.stay_date < arguments.snapshot_date - timedelta(days=7):
        parser.error("stay_date must be at least snapshot_date minus seven days")
    if arguments.stay_date > add_months(arguments.snapshot_date, 18):
        parser.error("stay_date must be within the 18-month source horizon")

    source = _source_facts(
        arguments.snapshot_date, arguments.hotel_code,
        arguments.stay_date, arguments.category,
    )
    application = _application_facts(
        arguments.snapshot_date, arguments.hotel_code,
        arguments.stay_date, arguments.category,
    )
    passed = source == application
    print(json.dumps({
        "passed": passed,
        "snapshotDate": arguments.snapshot_date,
        "hotelCode": arguments.hotel_code,
        "stayDate": arguments.stay_date,
        "category": arguments.category,
        "source": {"requestedRooms": source[0], "inventory": source[1]},
        "databaseA": {"requestedRooms": application[0], "inventory": application[1]},
    }, indent=2, default=_jsonable))
    if not passed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
