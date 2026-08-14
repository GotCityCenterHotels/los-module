"""Read-only LOS benchmark runner for integration_db."""

import argparse
import json

from queries.los_facts import LOS_FACTS_SQL
from psycopg.rows import tuple_row
from shared.db import get_export_connection


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-12-31")
    parser.add_argument(
        "--basis", choices=("sameDate", "sameWeekday"), default="sameDate"
    )
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")

    parameters = {
        "start_date": args.start,
        "end_date": args.end,
        "ly_comparison_basis": args.basis,
    }
    with get_export_connection() as connection:
        with connection.cursor(row_factory=tuple_row) as cursor:
            cursor.execute("SET LOCAL work_mem = '64MB'")
            for run in range(1, args.runs + 1):
                cursor.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, "
                    "SUMMARY, FORMAT JSON) " + LOS_FACTS_SQL,
                    parameters,
                )
                document = cursor.fetchone()[0][0]
                plan_nodes = list(_nodes(document["Plan"]))
                item_nodes = [
                    node for node in plan_nodes
                    if node.get("Relation Name") == "order_item_current"
                ]
                print(json.dumps({
                    "run": run,
                    "startDate": args.start,
                    "endDate": args.end,
                    "comparisonBasis": args.basis,
                    "planningMs": document.get("Planning Time"),
                    "executionMs": document.get("Execution Time"),
                    "sharedHits": document["Plan"].get("Shared Hit Blocks", 0),
                    "sharedReads": document["Plan"].get("Shared Read Blocks", 0),
                    "tempReads": document["Plan"].get("Temp Read Blocks", 0),
                    "tempWrites": document["Plan"].get("Temp Written Blocks", 0),
                    "orderItemLoops": sum(
                        node.get("Actual Loops", 0) for node in item_nodes
                    ),
                    "heapFetches": sum(
                        node.get("Heap Fetches", 0) for node in item_nodes
                    ),
                    "sortMethods": [
                        node.get("Sort Method") for node in plan_nodes
                        if node.get("Node Type") == "Sort"
                    ],
                }, separators=(",", ":")))


if __name__ == "__main__":
    main()
