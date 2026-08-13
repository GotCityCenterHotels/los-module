import argparse
import json
import sys

from calendar import monthrange
from datetime import date

from queries.supplement_source import (
    explain_latest_source_snapshot,
    explain_source_snapshot,
)


def add_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _walk_plan(node):
    yield node
    for child in node.get("Plans", []):
        yield from _walk_plan(child)


def _uses_bounded_access(plan):
    root = plan[0]["Plan"]
    nodes = list(_walk_plan(root))
    pruned = any(node.get("Subplans Removed", 0) > 0 for node in nodes)
    indexed = any(
        "Index" in node.get("Node Type", "")
        and "view_date" in str(node.get("Index Cond", "")).lower()
        for node in nodes
    )
    return pruned or indexed


def _uses_index_access(plan):
    return any(
        "Index" in node.get("Node Type", "")
        for node in _walk_plan(plan[0]["Plan"])
    )


def main():
    parser = argparse.ArgumentParser(
        description="Run the bounded Supplement source plan against read-only integration_db."
    )
    parser.add_argument("snapshot_date", type=date.fromisoformat)
    arguments = parser.parse_args()
    discovery_plan = explain_latest_source_snapshot()
    snapshot_plan = explain_source_snapshot(
        arguments.snapshot_date,
        add_months(arguments.snapshot_date, 18),
    )
    discovery_passed = _uses_index_access(discovery_plan)
    snapshot_passed = _uses_bounded_access(snapshot_plan)
    report = {
        "latestSnapshotDiscovery": discovery_plan,
        "boundedSnapshotRead": snapshot_plan,
        "checks": {
            "latestSnapshotUsesIndex": discovery_passed,
            "boundedReadPrunesOrUsesViewDateIndex": snapshot_passed,
        },
        "rolloutGate": "pass" if discovery_passed and snapshot_passed else "blocked",
    }
    print(json.dumps(report, indent=2, default=str))
    if report["rolloutGate"] == "blocked":
        print(
            "Production blocked: the bounded snapshot read did not demonstrate "
            "view_date partition pruning or index access.",
            file=sys.stderr,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
