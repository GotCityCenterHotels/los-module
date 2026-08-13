import argparse
import json
import sys

from calendar import monthrange
from datetime import date

from queries.supplement_source import (
    explain_booking_lifecycle,
    explain_inventory,
)


MAX_SMALL_SEQUENTIAL_SCAN_ROWS = 10_000


def add_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _walk_plan(node):
    yield node
    for child in node.get("Plans", []):
        yield from _walk_plan(child)


def _uses_bounded_access(plan, bounded_column="start_utc"):
    nodes = list(_walk_plan(plan[0]["Plan"]))
    bounded_column = bounded_column.lower()
    return any(
        (
            "Index" in node.get("Node Type", "")
            and bounded_column in str(node.get("Index Cond", "")).lower()
        )
        or node.get("Subplans Removed", 0) > 0
        for node in nodes
    )


def _uses_index_access(plan):
    return any(
        "Index" in node.get("Node Type", "")
        for node in _walk_plan(plan[0]["Plan"])
    )


def _has_broad_scan(plan, relation_names, include_index_scans=False):
    names = {name.lower() for name in relation_names}
    for node in _walk_plan(plan[0]["Plan"]):
        node_type = node.get("Node Type", "")
        if include_index_scans:
            if "Scan" not in node_type:
                continue
        elif node_type != "Seq Scan":
            continue
        if str(node.get("Relation Name", "")).lower() not in names:
            continue
        loops = int(node.get("Actual Loops") or 0)
        if loops == 0:
            continue
        scanned_per_loop = int(node.get("Actual Rows") or 0) + int(
            node.get("Rows Removed by Filter") or 0
        ) + int(node.get("Rows Removed by Index Recheck") or 0)
        if scanned_per_loop * loops > MAX_SMALL_SEQUENTIAL_SCAN_ROWS:
            return True
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Run the bounded Supplement source plan against read-only integration_db."
    )
    parser.add_argument("snapshot_date", type=date.fromisoformat)
    arguments = parser.parse_args()
    booking_plan = explain_booking_lifecycle(
        arguments.snapshot_date,
        add_months(arguments.snapshot_date, 18),
    )
    inventory_plan = explain_inventory(arguments.snapshot_date)
    booking_passed = (
        _uses_bounded_access(booking_plan, "start_utc")
        and not _has_broad_scan(booking_plan, {"order_item_current"})
    )
    inventory_passed = (
        _uses_index_access(inventory_plan)
        and not _has_broad_scan(inventory_plan, {
            "resource_history",
            "resource_category_history",
            "resource_category_assignment_history",
        }, include_index_scans=True)
    )
    report = {
        "boundedBookingLifecycleRead": booking_plan,
        "boundedInventoryRead": inventory_plan,
        "checks": {
            "bookingReadPrunesOrUsesStayDateIndex": booking_passed,
            "inventoryReadUsesIndex": inventory_passed,
        },
        "rolloutGate": "pass" if booking_passed and inventory_passed else "blocked",
    }
    print(json.dumps(report, indent=2, default=str))
    if report["rolloutGate"] == "blocked":
        print(
            "Production blocked: both direct source reads must demonstrate bounded "
            "index or partition access.",
            file=sys.stderr,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
