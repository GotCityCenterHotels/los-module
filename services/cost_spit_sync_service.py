"""Nightly publisher for the Cost Data SPIT read model.

SPIT here means "as the book stood on this same calendar date last year": a
reservation or item belongs in the snapshot when it was created on or before
that cutoff and was either never cancelled or cancelled after it. Bookings that
existed then and died later are therefore included, which is exactly what a
final-state read of the imported tables cannot reproduce - and it is why the
snapshot cannot be incremental. The cutoff moves every day, so every night asks
a different question and the whole window is rebuilt.

This follows the other read-model synchronizers: build an immutable run from the
read-only integration database, validate it, then move small publication
pointers in one Database A transaction. HTTP readers never observe a partial run
and never query the source database.

Rows are stored in exactly the shape and key case the HTTP response sends, as
json text rather than jsonb. Publication pays for that shape once a night so
that a read - which is what a user actually waits on - pays nothing for it.
"""

import logging
import os

from datetime import date, datetime, timedelta
from time import monotonic
from zoneinfo import ZoneInfo

from cost_database import apply_background_timeouts, cost_pool
from queries.cost_spit import COST_SPIT_DATASETS, COST_SPIT_SQL
from services.cost_schema_service import ensure_cost_settings_schema
from shared.comparison_dates import shift_cost_comparison_date
from shared.db import get_export_connection, get_import_connection
from shared.json_shape import camel_keys, compact_json


SYNC_LOCK_NAME = "functions.cost_spit_sync"
COMPARISON_BASES = ("sameDate", "sameWeekday")
SOURCE_BATCH_SIZE = int(os.environ.get("COST_SPIT_SOURCE_BATCH_SIZE", "5000"))
DAILY_INSERT_BATCH_SIZE = int(
    os.environ.get("COST_SPIT_DAILY_INSERT_BATCH_SIZE", "500")
)
# Match the LOS read model: keep one prior immutable publication for rollback
# while limiting the table and primary-index size of these full-year snapshots.
RUN_RETENTION = max(0, int(os.environ.get("COST_SPIT_RUN_RETENTION", "1")))

# The source query is the reason this read model exists, so it gets a ceiling
# suited to a background job rather than the five minute default shared with the
# short export statements. It is a ceiling, not a target: the nightly import has
# eight other datasets to get through inside one 30 minute function timeout.
SOURCE_STATEMENT_TIMEOUT_MS = int(
    os.environ.get("COST_SPIT_SOURCE_STATEMENT_TIMEOUT_MS", "900000")
)

# A run killed mid-stream - a function timeout, a redeploy - leaves committed
# daily rows behind a row that still says 'running', which retention skips
# because it only prunes finished runs. Holding the advisory lock means no other
# synchronization is live, so anything still 'running' and this old is abandoned.
ABANDONED_RUN_HOURS = int(os.environ.get("COST_SPIT_ABANDONED_RUN_HOURS", "6"))

# The Cost Data page opens on 1 January to today, so a request made in early
# January asks for a comparison range beginning in the previous calendar year. A
# lead-in before 1 January keeps those covered. It is deliberately modest
# because every extra day is another day of source lifecycle scan on a job that
# shares a 30 minute budget with eight other datasets.
COVERAGE_LEAD_IN_DAYS = int(
    os.environ.get("COST_SPIT_COVERAGE_LEAD_IN_DAYS", "45")
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")


INSERT_DAILY_SQL = """
INSERT INTO functions.cost_spit_daily (
    run_id, comparison_basis, stay_date, dataset, fact_count, fact_rows
) VALUES (%s, %s, %s, %s, %s, %s::json)
"""


def stockholm_today():
    return datetime.now(STOCKHOLM).date()


def coverage_window(as_of_date):
    """The current-year span that the two snapshots are shifted from."""
    return (
        date(as_of_date.year, 1, 1) - timedelta(days=COVERAGE_LEAD_IN_DAYS),
        date(as_of_date.year, 12, 31),
    )


def snapshot_plan(as_of_date):
    """The two snapshots covering the Cost Data page's comparison window."""
    current_start, current_end = coverage_window(as_of_date)
    return {
        basis: {
            "cutoff_date": shift_cost_comparison_date(as_of_date, basis),
            "start_date": shift_cost_comparison_date(current_start, basis),
            "end_date": shift_cost_comparison_date(current_end, basis),
        }
        for basis in COMPARISON_BASES
    }


def _shape_fact(payload):
    """One source row in exactly the shape the HTTP response sends.

    camelCase because that is the wire contract, and without nulls because the
    only null these datasets carry is last_updated_at, which the lifecycle
    source cannot know and the browser already reads as absent. Both happen
    here, once a night, so a read can hand the stored text straight to the body.
    """
    names = camel_keys(payload.keys())
    return {
        name: value
        for name, value in zip(names, payload.values())
        if value is not None
    }


def _create_run(as_of_date):
    current_start, current_end = coverage_window(as_of_date)
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            apply_background_timeouts(cursor)
            cursor.execute(
                """
                DELETE FROM functions.cost_spit_sync_runs
                WHERE status = 'running'
                  AND started_at < now() - make_interval(hours => %s)
                """,
                (ABANDONED_RUN_HOURS,),
            )
            if cursor.rowcount:
                logging.warning(
                    "Discarded %d abandoned Cost SPIT run(s)", cursor.rowcount
                )
            cursor.execute(
                """
                INSERT INTO functions.cost_spit_sync_runs (
                    status, source_as_of_date,
                    current_range_start, current_range_end
                ) VALUES ('running', %s, %s, %s)
                RETURNING run_id
                """,
                (as_of_date, current_start, current_end),
            )
            return cursor.fetchone()[0]


def _write_daily_batch(connection, rows):
    if not rows:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(INSERT_DAILY_SQL, rows)
    connection.commit()
    return len(rows)


def _stream_basis(run_id, basis, plan):
    """Stream one source result and persist one compact array per dataset/day."""
    exported = 0
    imported = 0
    pending_daily = []
    current_key = None
    current_rows = []

    def finish_group(target_connection):
        nonlocal current_key, current_rows, imported, pending_daily
        if current_key is None:
            return
        dataset, stay_date = current_key
        pending_daily.append(
            (
                run_id,
                basis,
                stay_date,
                dataset,
                len(current_rows),
                compact_json(current_rows),
            )
        )
        current_key = None
        current_rows = []
        if len(pending_daily) >= DAILY_INSERT_BATCH_SIZE:
            imported += _write_daily_batch(target_connection, pending_daily)
            pending_daily = []

    parameters = {
        "start_date": plan["start_date"],
        "end_date": plan["end_date"],
        "cutoff_date": plan["cutoff_date"],
    }
    with get_export_connection(SOURCE_STATEMENT_TIMEOUT_MS) as source_connection:
        with source_connection.cursor() as setup_cursor:
            setup_cursor.execute("SET LOCAL work_mem = '64MB'")
        with get_import_connection() as target_connection:
            with source_connection.cursor(name=f"cost_spit_{basis.lower()}") as source:
                source.itersize = SOURCE_BATCH_SIZE
                source.execute(COST_SPIT_SQL, parameters)
                while True:
                    rows = source.fetchmany(SOURCE_BATCH_SIZE)
                    if not rows:
                        break
                    for row in rows:
                        dataset = row["dataset"]
                        payload = row["payload"] or {}
                        if dataset not in COST_SPIT_DATASETS:
                            raise RuntimeError(
                                f"Unknown Cost SPIT source dataset: {dataset}"
                            )
                        stay_date = date.fromisoformat(payload["stay_date"])
                        if not plan["start_date"] <= stay_date <= plan["end_date"]:
                            raise RuntimeError(
                                "Cost SPIT source returned a row outside coverage"
                            )
                        key = (dataset, stay_date)
                        if current_key is not None and key != current_key:
                            finish_group(target_connection)
                        current_key = key
                        current_rows.append(_shape_fact(payload))
                        exported += 1

                finish_group(target_connection)
                imported += _write_daily_batch(target_connection, pending_daily)

    return exported, imported


def _publish(run_id, as_of_date, plan, source_rows, daily_rows):
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            apply_background_timeouts(cursor)
            cursor.execute(
                """
                SELECT comparison_basis, count(*), coalesce(sum(fact_count), 0)
                FROM functions.cost_spit_daily
                WHERE run_id = %s
                GROUP BY comparison_basis
                """,
                (run_id,),
            )
            summary = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
            stored_daily = sum(count for count, _facts in summary.values())
            stored_facts = sum(facts for _count, facts in summary.values())
            # stored_facts against source_rows is the one that matters: it says
            # every row the source produced survived into a published array, not
            # merely that the right number of arrays exist.
            if (
                source_rows <= 0
                or daily_rows <= 0
                or stored_daily != daily_rows
                or stored_facts != source_rows
                or set(summary) != set(COMPARISON_BASES)
                or any(count <= 0 for count, _facts in summary.values())
            ):
                raise RuntimeError(
                    "Cost SPIT staging validation failed: "
                    f"source_rows={source_rows} daily_rows={daily_rows} "
                    f"stored_daily={stored_daily} stored_facts={stored_facts} "
                    f"bases={summary}"
                )

            for basis in COMPARISON_BASES:
                basis_plan = plan[basis]
                cursor.execute(
                    """
                    INSERT INTO functions.cost_spit_publication (
                        comparison_basis, run_id, cutoff_date,
                        minimum_stay_date, maximum_stay_date, published_at
                    ) VALUES (%s, %s, %s, %s, %s, clock_timestamp())
                    ON CONFLICT (comparison_basis) DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        cutoff_date = EXCLUDED.cutoff_date,
                        minimum_stay_date = EXCLUDED.minimum_stay_date,
                        maximum_stay_date = EXCLUDED.maximum_stay_date,
                        published_at = EXCLUDED.published_at
                    """,
                    (
                        basis,
                        run_id,
                        basis_plan["cutoff_date"],
                        basis_plan["start_date"],
                        basis_plan["end_date"],
                    ),
                )

            cursor.execute(
                """
                UPDATE functions.cost_spit_sync_runs
                SET status = 'published', source_as_of_date = %s,
                    source_rows = %s, daily_rows = %s, fact_rows = %s,
                    finished_at = clock_timestamp(),
                    published_at = clock_timestamp()
                WHERE run_id = %s AND status = 'running'
                """,
                (as_of_date, source_rows, daily_rows, stored_facts, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Cost SPIT run was not publishable")

            cursor.execute(
                """
                DELETE FROM functions.cost_spit_sync_runs old_run
                WHERE old_run.run_id IN (
                    SELECT run_id
                    FROM functions.cost_spit_sync_runs
                    WHERE status IN ('published', 'failed')
                      AND run_id <> %s
                    ORDER BY run_id DESC
                    OFFSET %s
                )
                """,
                (run_id, RUN_RETENTION),
            )


def _mark_failed(run_id, error):
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE functions.cost_spit_sync_runs
                SET status = 'failed', finished_at = clock_timestamp(),
                    error_message = %s
                WHERE run_id = %s AND status = 'running'
                """,
                (str(error).splitlines()[0][:2000], run_id),
            )


def sync_cost_spit(as_of_date=None):
    """Build and atomically publish both bases for the coverage window."""
    ensure_cost_settings_schema()
    as_of_date = as_of_date or stockholm_today()
    plan = snapshot_plan(as_of_date)

    # A dedicated connection rather than a pooled one. This lock is held for the
    # whole build, which is minutes, and the pool it used to borrow from is four
    # wide with the Cost Data read path already capped at three - so holding one
    # here left nothing for the schema check and publication lookup that every
    # page request makes before it reads anything.
    with get_import_connection() as lock_connection:
        lock_connection.autocommit = True
        with lock_connection.cursor() as lock_cursor:
            lock_cursor.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                (SYNC_LOCK_NAME,),
            )
            if not lock_cursor.fetchone()[0]:
                raise RuntimeError("A Cost SPIT synchronization is already running")

        run_id = None
        started = monotonic()
        try:
            run_id = _create_run(as_of_date)
            source_rows = 0
            daily_rows = 0
            for basis in COMPARISON_BASES:
                exported, imported = _stream_basis(run_id, basis, plan[basis])
                source_rows += exported
                daily_rows += imported

            _publish(run_id, as_of_date, plan, source_rows, daily_rows)
            result = {
                "status": "success",
                "runId": run_id,
                "sourceAsOfDate": as_of_date.isoformat(),
                "export_rows": source_rows,
                "import_rows": daily_rows,
                "pruned_rows": 0,
                "durationSeconds": round(monotonic() - started, 3),
            }
            logging.info("Cost SPIT synchronization published %s", result)
            return result
        except Exception as error:
            if run_id is not None:
                _mark_failed(run_id, error)
            logging.exception("Cost SPIT synchronization failed")
            raise
        finally:
            with lock_connection.cursor() as lock_cursor:
                lock_cursor.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))",
                    (SYNC_LOCK_NAME,),
                )
