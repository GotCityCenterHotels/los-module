"""Find out where the Cost Data SPIT source query actually spends its time.

The nightly publisher runs one lifecycle query per comparison basis over a
calendar year of order_item_current. When that job is slow, the useful question
is not "is it slow" but "which node is slow", and the answer is in the plan
rather than in anyone's reading of the SQL.

    python profile_cost_spit_source.py                  # plan only, seconds
    python profile_cost_spit_source.py --analyze        # really runs it
    python profile_cost_spit_source.py --analyze --basis sameDate

Plan-only is the default because it costs nothing: it is enough to show a
sequential scan where an index was assumed, a nested loop over a text-cast join
key, or a sort that the planner already expects to spill to disk. Use --analyze
when the plan looks reasonable and the job is slow anyway; that one executes the
query for real, so give it the same statement ceiling the publisher uses.
"""

import argparse
import json
import os
import sys

from datetime import date
from pathlib import Path


def load_local_settings():
    """Take connection settings from local.settings.json, as the host would.

    Every script here reads os.environ, which is right for the deployed app -
    its settings come from the Function App configuration. Run from a terminal
    there is nothing to read, so this loads the same file the Functions host
    loads, and which .gitignore already keeps out of the repository. Existing
    environment variables win, so exporting one to try something still works.
    """
    settings = Path(__file__).resolve().parent / "local.settings.json"
    if not settings.exists():
        return False
    # utf-8-sig, not utf-8: Windows PowerShell's Out-File writes UTF-8 with a
    # byte order mark, and a file produced that way would otherwise fail to
    # parse on its first character. Reading it this way costs nothing when the
    # mark is absent.
    values = json.loads(settings.read_text(encoding="utf-8-sig")).get("Values", {})
    for name, value in values.items():
        if value is not None:
            os.environ.setdefault(name, str(value))
    return True


LOADED_LOCAL_SETTINGS = load_local_settings()

# This script reads Database B and nothing else, but importing the publisher
# builds Database A's connection pool, which insists on its settings at import
# time. Placeholders keep the requirement honest: only the integration database
# credentials below have to be real, and nothing here ever opens the pool.
for _name, _placeholder in (
    ("COST_DB_NAME", "unused-by-this-script"),
    ("COST_DB_HOST", "localhost"),
    ("COST_DB_USER", "unused"),
    ("COST_DB_PASSWORD", "unused"),
):
    os.environ.setdefault(_name, _placeholder)

from queries.cost_spit import COST_SPIT_SQL  # noqa: E402
from services.cost_spit_sync_service import (  # noqa: E402
    COMPARISON_BASES,
    SOURCE_STATEMENT_TIMEOUT_MS,
    snapshot_plan,
    stockholm_today,
)
from shared.db import get_export_connection  # noqa: E402


def walk(node):
    yield node
    for child in node.get("Plans", []):
        yield from walk(child)


def describe(node):
    name = node.get("Node Type", "?")
    for key in ("Relation Name", "CTE Name", "Index Name"):
        if node.get(key):
            name = f"{name} on {node[key]}"
            break
    return name


def report(plan, analyzed):
    root = plan[0]["Plan"]
    nodes = list(walk(root))

    if analyzed:
        print(f"\n  execution time: {plan[0].get('Execution Time', 0)/1000:.1f}s")
        print(f"  planning time:  {plan[0].get('Planning Time', 0)/1000:.1f}s")

        # Self time, not inclusive time: a node whose children are the expensive
        # ones is not itself the problem, and ranking by inclusive time puts the
        # root on top every time and says nothing.
        inclusive = {id(n): n.get("Actual Total Time", 0) * max(n.get("Actual Loops", 1), 1)
                     for n in nodes}
        rows = []
        for node in nodes:
            own = inclusive[id(node)] - sum(
                inclusive[id(child)] for child in node.get("Plans", [])
            )
            rows.append((own, node))
        rows.sort(key=lambda entry: entry[0], reverse=True)

        print("\n  slowest nodes by self time")
        for own, node in rows[:12]:
            print(
                f"    {own/1000:8.1f}s  {describe(node):<44}"
                f" rows={node.get('Actual Rows', 0):>9}"
                f" loops={node.get('Actual Loops', 1):>5}"
            )
    else:
        print("\n  estimated cost: %.0f, estimated rows: %s"
              % (root.get("Total Cost", 0), root.get("Plan Rows", 0)))

    # Only scans big enough to matter. A dimension table of nine enterprises is
    # read sequentially because that is the right way to read nine rows, and
    # listing those buries the one scan worth looking at.
    scans = [
        n for n in nodes
        if n.get("Node Type") == "Seq Scan"
        and max(n.get("Actual Rows", 0), n.get("Plan Rows", 0)) >= 10_000
    ]
    if scans:
        print("\n  sequential scans over 10k rows")
        for node in scans:
            actual = node.get("Actual Rows")
            size = f"rows={actual}" if actual is not None else f"est={node.get('Plan Rows')}"
            print(f"    {describe(node):<40} {size:>14}"
                  f"  loops={node.get('Actual Loops', 1)}")

    # A plan built on a row count that is wrong by orders of magnitude is how a
    # query that looks cheap runs for minutes: the planner picks nested loops
    # for what it thinks is a handful of rows and then does it a quarter of a
    # million times.
    if analyzed:
        misestimates = []
        for node in nodes:
            planned = node.get("Plan Rows", 0) * max(node.get("Actual Loops", 1), 1)
            got = node.get("Actual Rows", 0) * max(node.get("Actual Loops", 1), 1)
            if got >= 10_000 and planned and got / planned >= 10:
                misestimates.append((got / planned, planned, got, node))
        if misestimates:
            misestimates.sort(reverse=True, key=lambda entry: entry[0])
            print("\n  row estimates the planner got wrong by 10x or more")
            for ratio, planned, got, node in misestimates[:8]:
                print(f"    {ratio:7.0f}x under  {describe(node):<38}"
                      f" planned={planned:>9} actual={got:>9}")

    spills = [n for n in nodes if n.get("Sort Space Type") == "Disk"
              or n.get("Peak Memory Usage", 0) > 60_000]
    if spills:
        print("\n  sorts or hashes at the edge of work_mem")
        for node in spills:
            print(f"    {describe(node):<48}"
                  f" {node.get('Sort Space Type', 'Memory')}"
                  f" {node.get('Sort Space Used', node.get('Peak Memory Usage', 0))}kB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basis", choices=COMPARISON_BASES, default=None,
                        help="Profile one basis instead of both.")
    parser.add_argument("--analyze", action="store_true",
                        help="Execute the query and report real timings.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="Stand in for today, to reproduce a past night.")
    parser.add_argument("--work-mem", default="64MB",
                        help="Match the publisher's setting (default 64MB).")
    arguments = parser.parse_args()

    as_of = arguments.as_of or stockholm_today()
    plan = snapshot_plan(as_of)
    bases = [arguments.basis] if arguments.basis else list(COMPARISON_BASES)

    mode = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)" if arguments.analyze \
        else "EXPLAIN (FORMAT JSON)"
    print(f"as of {as_of}  |  {'measured' if arguments.analyze else 'planned'}"
          f"  |  settings from "
          f"{'local.settings.json' if LOADED_LOCAL_SETTINGS else 'environment'}")

    for basis in bases:
        window = plan[basis]
        span = (window["end_date"] - window["start_date"]).days + 1
        print(f"\n{'=' * 72}\n{basis}: {window['start_date']}..{window['end_date']}"
              f" ({span} days), cutoff {window['cutoff_date']}")

        with get_export_connection(SOURCE_STATEMENT_TIMEOUT_MS) as connection:
            with connection.cursor() as cursor:
                # integration_db is read-only at the role and at the session,
                # and this script only ever issues EXPLAIN over a SELECT. Assert
                # the session half rather than trusting it: if the connection
                # settings ever drift, this stops here instead of pointing a
                # writable session at the source database.
                cursor.execute("SHOW transaction_read_only")
                readonly = cursor.fetchone()
                readonly = readonly["transaction_read_only"] \
                    if isinstance(readonly, dict) else readonly[0]
                if readonly != "on":
                    raise SystemExit(
                        "Refusing to run: the integration_db session is not "
                        f"read-only (transaction_read_only={readonly})"
                    )
                cursor.execute(f"SET LOCAL work_mem = '{arguments.work_mem}'")
                cursor.execute(f"{mode} {COST_SPIT_SQL}", {
                    "start_date": window["start_date"],
                    "end_date": window["end_date"],
                    "cutoff_date": window["cutoff_date"],
                })
                row = cursor.fetchone()
        explained = row["QUERY PLAN"] if isinstance(row, dict) else row[0]
        if isinstance(explained, str):
            explained = json.loads(explained)
        report(explained, arguments.analyze)

    return 0


if __name__ == "__main__":
    sys.exit(main())
