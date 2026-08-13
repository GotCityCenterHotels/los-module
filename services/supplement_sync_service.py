import logging

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from time import perf_counter

from psycopg.rows import dict_row

from cost_database import cost_pool
from queries.supplement_source import (
    fetch_latest_source_snapshot,
    fetch_source_snapshot_dates,
    iter_source_snapshot_batches,
)
from services.supplement_schema_service import ensure_supplement_schema


SYNC_LOCK_NAME = "functions.supplement_sync"
SOURCE_OVERLAP_DAYS = 3
BATCH_SIZE = 5000

STAGING_INSERT_SQL = """
    INSERT INTO supplement_snapshot_stage (
        snapshot_date, stay_date, hotel_code, space_room_name,
        requested_room_name, assigned_rooms, room_revenue,
        total_space, space_to_sell
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def add_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def _stage_row(row):
    return (
        row["snapshot_date"],
        row["stay_date"],
        (row["hotel_code"] or "").strip(),
        (row["space_room_name"] or "").strip(),
        (row["requested_room_name"] or "").strip(),
        row["assigned_rooms"] or Decimal(0),
        row["room_revenue"] or Decimal(0),
        row["total_space"] or Decimal(0),
        row["space_to_sell"] or Decimal(0),
    )


def _create_stage(cursor):
    cursor.execute("""
        CREATE TEMP TABLE supplement_snapshot_stage (
            snapshot_date date NOT NULL,
            stay_date date NOT NULL,
            hotel_code text NOT NULL,
            space_room_name text NOT NULL,
            requested_room_name text NOT NULL,
            assigned_rooms numeric NOT NULL,
            room_revenue numeric NOT NULL,
            total_space numeric NOT NULL,
            space_to_sell numeric NOT NULL
        ) ON COMMIT DROP
    """)


def _validate_stage(cursor, expected_snapshots):
    cursor.execute("SELECT count(*) AS row_count FROM supplement_snapshot_stage")
    row_count = cursor.fetchone()["row_count"]
    if row_count == 0:
        raise ValueError("Supplement source returned no rows for the requested snapshots")

    cursor.execute("""
        SELECT count(*) AS invalid_count
        FROM supplement_snapshot_stage
        WHERE nullif(trim(hotel_code), '') IS NULL
           OR nullif(trim(space_room_name), '') IS NULL
           OR nullif(trim(requested_room_name), '') IS NULL
           OR assigned_rooms < 0
           OR room_revenue < 0
           OR total_space < 0
           OR space_to_sell < 0
    """)
    invalid_count = cursor.fetchone()["invalid_count"]
    if invalid_count:
        raise ValueError(f"Supplement source contains {invalid_count} invalid rows")

    cursor.execute("""
        SELECT count(*) AS duplicate_groups
        FROM (
            SELECT snapshot_date, stay_date, hotel_code, space_room_name,
                   requested_room_name
            FROM supplement_snapshot_stage
            GROUP BY snapshot_date, stay_date, hotel_code, space_room_name,
                     requested_room_name
            HAVING count(*) > 1
        ) duplicates
    """)
    duplicate_groups = cursor.fetchone()["duplicate_groups"]
    if duplicate_groups:
        raise ValueError(
            f"Supplement source contains {duplicate_groups} duplicate fact keys"
        )

    cursor.execute("SELECT DISTINCT snapshot_date FROM supplement_snapshot_stage")
    staged_snapshots = {row["snapshot_date"] for row in cursor.fetchall()}
    missing = sorted(set(expected_snapshots) - staged_snapshots)
    if missing:
        raise ValueError(
            "Supplement source returned no rows for snapshots: "
            + ", ".join(value.isoformat() for value in missing)
        )

    cursor.execute("""
        SELECT s.snapshot_date,
               count(DISTINCT (s.stay_date, s.hotel_code, s.space_room_name,
                               s.requested_room_name)) AS staged_count,
               coalesce(existing.row_count, 0) AS existing_count
        FROM supplement_snapshot_stage s
        LEFT JOIN (
            SELECT snapshot_date, count(*) AS row_count
            FROM functions.supplement_snapshot_detail
            WHERE snapshot_date = ANY(%s)
            GROUP BY snapshot_date
        ) existing USING (snapshot_date)
        GROUP BY s.snapshot_date, existing.row_count
    """, (list(expected_snapshots),))
    for row in cursor.fetchall():
        existing_count = row["existing_count"]
        if existing_count < 100:
            continue
        ratio = row["staged_count"] / existing_count
        if ratio < 0.5 or ratio > 1.5:
            raise ValueError(
                f"Supplement row-count variance is too large for {row['snapshot_date']}: "
                f"existing={existing_count} staged={row['staged_count']}"
            )
    return row_count


def _publish_stage(cursor, run_id, snapshot_dates):
    first_snapshot = min(snapshot_dates)
    last_snapshot = max(snapshot_dates)
    cursor.execute(
        "SELECT functions.ensure_supplement_month_partitions(%s)",
        (first_snapshot,),
    )
    month_cursor = date(first_snapshot.year, first_snapshot.month, 1)
    final_month = date(last_snapshot.year, last_snapshot.month, 1)
    while month_cursor < final_month:
        month_cursor = add_months(month_cursor, 1)
        cursor.execute(
            "SELECT functions.ensure_supplement_month_partitions(%s)",
            (month_cursor,),
        )

    # A repair range may contain calendar dates that do not exist in the source.
    # Replace only snapshots that were actually discovered and fully staged.
    parameters = {"snapshot_dates": list(snapshot_dates)}
    for table in (
        "supplement_snapshot_detail",
        "supplement_snapshot_category",
        "supplement_snapshot_inventory",
    ):
        cursor.execute(
            f"DELETE FROM functions.{table} "
            "WHERE snapshot_date = ANY(%(snapshot_dates)s)",
            parameters,
        )

    cursor.execute("""
        INSERT INTO functions.supplement_snapshot_detail (
            snapshot_date, stay_date, hotel_code, space_room_name,
            requested_room_name, assigned_rooms, room_revenue, currency, run_id
        )
        SELECT snapshot_date, stay_date, hotel_code, space_room_name,
               requested_room_name, sum(assigned_rooms), sum(room_revenue), 'SEK', %s
        FROM supplement_snapshot_stage
        GROUP BY snapshot_date, stay_date, hotel_code,
                 space_room_name, requested_room_name
    """, (run_id,))
    cursor.execute("""
        INSERT INTO functions.supplement_snapshot_category (
            snapshot_date, stay_date, hotel_code, space_room_name,
            assigned_rooms, room_revenue, currency, run_id
        )
        SELECT snapshot_date, stay_date, hotel_code, space_room_name,
               sum(assigned_rooms), sum(room_revenue), 'SEK', %s
        FROM supplement_snapshot_stage
        GROUP BY snapshot_date, stay_date, hotel_code, space_room_name
    """, (run_id,))
    cursor.execute("""
        INSERT INTO functions.supplement_snapshot_inventory (
            snapshot_date, stay_date, hotel_code, space_room_name,
            total_space, space_to_sell, run_id
        )
        SELECT snapshot_date, stay_date, hotel_code, space_room_name,
               max(total_space), max(space_to_sell), %s
        FROM supplement_snapshot_stage
        GROUP BY snapshot_date, stay_date, hotel_code, space_room_name
    """, (run_id,))

    cursor.execute("""
        INSERT INTO functions.supplement_hotels (hotel_code, hotel_name, last_seen_at)
        SELECT DISTINCT hotel_code, hotel_code, now()
        FROM supplement_snapshot_stage
        ON CONFLICT (hotel_code) DO UPDATE SET
            active = true,
            last_seen_at = now()
    """)
    cursor.execute("""
        INSERT INTO functions.supplement_room_categories (
            hotel_code, space_room_name, short_name, sort_order, last_seen_at
        )
        SELECT DISTINCT hotel_code, space_room_name,
               left(upper(space_room_name), 8), 0, now()
        FROM supplement_snapshot_stage
        ON CONFLICT (hotel_code, space_room_name) DO UPDATE SET last_seen_at = now()
    """)

    cursor.execute("""
        SELECT min(stay_date) AS minimum_stay_date,
               max(stay_date) AS maximum_stay_date
        FROM supplement_snapshot_stage
    """)
    staged_range = cursor.fetchone()
    minimum_stay_date = staged_range["minimum_stay_date"]
    maximum_stay_date = staged_range["maximum_stay_date"]
    rebuild_parameters = {
        "minimum_stay_date": minimum_stay_date,
        "maximum_stay_date": maximum_stay_date,
    }
    for table in (
        "supplement_latest_detail",
        "supplement_latest_category",
        "supplement_latest_inventory",
    ):
        cursor.execute(
            f"DELETE FROM functions.{table} "
            "WHERE stay_date BETWEEN %(minimum_stay_date)s AND %(maximum_stay_date)s",
            rebuild_parameters,
        )

    cursor.execute("""
        INSERT INTO functions.supplement_latest_inventory (
            stay_date, hotel_code, space_room_name, snapshot_date,
            total_space, space_to_sell, run_id
        )
        WITH chosen_stays AS (
            SELECT stay_date, hotel_code, max(snapshot_date) AS snapshot_date
            FROM functions.supplement_snapshot_inventory
            WHERE stay_date BETWEEN %(minimum_stay_date)s AND %(maximum_stay_date)s
            GROUP BY stay_date, hotel_code
        )
        SELECT i.stay_date, i.hotel_code, i.space_room_name, i.snapshot_date,
               i.total_space, i.space_to_sell, i.run_id
        FROM functions.supplement_snapshot_inventory i
        JOIN chosen_stays s USING (stay_date, hotel_code, snapshot_date)
    """, rebuild_parameters)
    cursor.execute("""
        INSERT INTO functions.supplement_latest_category (
            stay_date, hotel_code, space_room_name, snapshot_date,
            assigned_rooms, room_revenue, currency, run_id
        )
        SELECT c.stay_date, c.hotel_code, c.space_room_name, c.snapshot_date,
               c.assigned_rooms, c.room_revenue, c.currency, c.run_id
        FROM functions.supplement_snapshot_category c
        JOIN functions.supplement_latest_inventory i
          ON i.stay_date = c.stay_date
         AND i.hotel_code = c.hotel_code
         AND i.space_room_name = c.space_room_name
         AND i.snapshot_date = c.snapshot_date
        WHERE c.stay_date BETWEEN %(minimum_stay_date)s AND %(maximum_stay_date)s
    """, rebuild_parameters)
    cursor.execute("""
        INSERT INTO functions.supplement_latest_detail (
            stay_date, hotel_code, space_room_name, requested_room_name,
            snapshot_date, assigned_rooms, room_revenue, currency, run_id
        )
        SELECT d.stay_date, d.hotel_code, d.space_room_name, d.requested_room_name,
               d.snapshot_date, d.assigned_rooms, d.room_revenue, d.currency, d.run_id
        FROM functions.supplement_snapshot_detail d
        JOIN functions.supplement_latest_inventory i
          ON i.stay_date = d.stay_date
         AND i.hotel_code = d.hotel_code
         AND i.space_room_name = d.space_room_name
         AND i.snapshot_date = d.snapshot_date
        WHERE d.stay_date BETWEEN %(minimum_stay_date)s AND %(maximum_stay_date)s
    """, rebuild_parameters)

    cursor.execute("""
        INSERT INTO functions.supplement_coverage (
            singleton, minimum_stay_date, maximum_stay_date,
            minimum_snapshot_date, maximum_snapshot_date, updated_at
        )
        SELECT true, min(stay_date), max(stay_date),
               min(snapshot_date), max(snapshot_date), now()
        FROM functions.supplement_snapshot_inventory
        ON CONFLICT (singleton) DO UPDATE SET
            minimum_stay_date = EXCLUDED.minimum_stay_date,
            maximum_stay_date = EXCLUDED.maximum_stay_date,
            minimum_snapshot_date = EXCLUDED.minimum_snapshot_date,
            maximum_snapshot_date = EXCLUDED.maximum_snapshot_date,
            updated_at = now()
    """)


def _published_snapshot(cursor):
    cursor.execute("SELECT data_as_of FROM functions.supplement_publication WHERE singleton")
    row = cursor.fetchone()
    return row["data_as_of"] if row else None


def _apply_retention(cursor, reference_date):
    minimum_stay_date = add_months(reference_date, -48)
    maximum_stay_date = add_months(reference_date, 18)
    for table in (
        "supplement_snapshot_detail",
        "supplement_snapshot_category",
        "supplement_snapshot_inventory",
    ):
        cursor.execute(f"""
            DELETE FROM functions.{table}
            WHERE stay_date < %s OR stay_date > %s
               OR snapshot_date < (stay_date - 366)
               OR snapshot_date > (stay_date + 7)
        """, (minimum_stay_date, maximum_stay_date))
    # Latest rows become final facts after departure and are deliberately kept
    # permanently. Only the denser pickup snapshots are subject to retention.
    cursor.execute("""
        INSERT INTO functions.supplement_coverage (
            singleton, minimum_stay_date, maximum_stay_date,
            minimum_snapshot_date, maximum_snapshot_date, updated_at
        )
        SELECT true, min(stay_date), max(stay_date),
               min(snapshot_date), max(snapshot_date), now()
        FROM functions.supplement_snapshot_inventory
        ON CONFLICT (singleton) DO UPDATE SET
            minimum_stay_date = EXCLUDED.minimum_stay_date,
            maximum_stay_date = EXCLUDED.maximum_stay_date,
            minimum_snapshot_date = EXCLUDED.minimum_snapshot_date,
            maximum_snapshot_date = EXCLUDED.maximum_snapshot_date,
            updated_at = now()
    """)


def sync_supplement(mode="delta", snapshot_from=None, snapshot_to=None):
    if mode not in {"delta", "repair", "backfill"}:
        raise ValueError("mode must be delta, repair, or backfill")
    started_at = perf_counter()
    ensure_supplement_schema()

    with cost_pool.connection() as app_connection:
        with app_connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired", (SYNC_LOCK_NAME,))
            if not cursor.fetchone()["acquired"]:
                raise RuntimeError("Another Supplement synchronization is already running")

            run_id = None
            try:
                latest_source = fetch_latest_source_snapshot()
                if latest_source is None:
                    raise RuntimeError("integration_db has no Supplement snapshots")

                published = _published_snapshot(cursor)
                if mode == "delta":
                    snapshot_to = latest_source
                    snapshot_from = (
                        max(published - timedelta(days=SOURCE_OVERLAP_DAYS - 1), date.min)
                        if published
                        else latest_source
                    )
                else:
                    if snapshot_from is None or snapshot_to is None:
                        raise ValueError("repair and backfill require snapshotFrom and snapshotTo")
                    if snapshot_from > snapshot_to:
                        raise ValueError("snapshotFrom cannot be after snapshotTo")
                    if snapshot_to > latest_source:
                        raise ValueError("snapshotTo cannot be newer than the source watermark")

                cursor.execute("""
                    INSERT INTO functions.supplement_sync_runs (
                        mode, status, source_snapshot_from, source_snapshot_to
                    ) VALUES (%s, 'running', %s, %s)
                    RETURNING run_id
                """, (mode, snapshot_from, snapshot_to))
                run_id = cursor.fetchone()["run_id"]
                app_connection.commit()

                source_snapshots = fetch_source_snapshot_dates(snapshot_from, snapshot_to)
                if not source_snapshots:
                    raise RuntimeError("integration_db has no snapshots in the requested range")

                _create_stage(cursor)
                exported_rows = 0
                for snapshot_date in source_snapshots:
                    for rows in iter_source_snapshot_batches(
                        snapshot_date,
                        add_months(snapshot_date, 18),
                        BATCH_SIZE,
                    ):
                        cursor.executemany(STAGING_INSERT_SQL, [_stage_row(row) for row in rows])
                        exported_rows += len(rows)

                imported_rows = _validate_stage(cursor, source_snapshots)
                _publish_stage(cursor, run_id, source_snapshots)
                publication_date = max(published or snapshot_to, max(source_snapshots))
                if mode == "delta" and publication_date.day == 1:
                    _apply_retention(cursor, publication_date)
                cursor.execute("""
                    INSERT INTO functions.supplement_publication (
                        singleton, run_id, data_as_of, published_at
                    ) VALUES (true, %s, %s, now())
                    ON CONFLICT (singleton) DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        data_as_of = EXCLUDED.data_as_of,
                        published_at = now()
                """, (run_id, publication_date))
                cursor.execute("""
                    UPDATE functions.supplement_sync_runs
                    SET status = 'published', exported_rows = %s, imported_rows = %s,
                        finished_at = now(), published_at = now()
                    WHERE run_id = %s
                """, (exported_rows, imported_rows, run_id))
                app_connection.commit()
                logging.info(
                    "Supplement sync published run_id=%s mode=%s snapshots=%s..%s "
                    "source_rows=%s app_rows=%s elapsed_seconds=%.2f",
                    run_id, mode, snapshot_from, snapshot_to, exported_rows,
                    imported_rows, perf_counter() - started_at,
                )
                return {
                    "status": "published",
                    "runId": run_id,
                    "mode": mode,
                    "snapshotFrom": snapshot_from.isoformat(),
                    "snapshotTo": snapshot_to.isoformat(),
                    "exportedRows": exported_rows,
                    "importedRows": imported_rows,
                }
            except Exception as error:
                app_connection.rollback()
                if run_id is not None:
                    cursor.execute("""
                        UPDATE functions.supplement_sync_runs
                        SET status = 'failed', finished_at = now(), error_message = %s
                        WHERE run_id = %s
                    """, (str(error)[:2000], run_id))
                    app_connection.commit()
                logging.exception("Supplement synchronization failed mode=%s", mode)
                raise
            finally:
                cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (SYNC_LOCK_NAME,))


def run_backfill_partition(snapshot_date):
    return sync_supplement("backfill", snapshot_date, snapshot_date)
