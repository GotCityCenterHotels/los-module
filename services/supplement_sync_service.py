import logging

from calendar import monthrange
from datetime import date, timedelta
from time import perf_counter

from psycopg.rows import dict_row

from cost_database import cost_pool
from queries.supplement_source import (
    iter_booking_lifecycle_batches,
    iter_inventory_batches,
    snapshot_dates,
    stockholm_today,
)
from services.supplement_schema_service import ensure_supplement_schema


SYNC_LOCK_NAME = "functions.supplement_sync"
SOURCE_OVERLAP_DAYS = 4
BATCH_SIZE = 5000


def _inventory_variance_exceeds(previous_count, staged_count):
    if not previous_count:
        return False
    return staged_count < previous_count * 0.5 or staged_count > previous_count * 1.5

BOOKING_STAGE_INSERT_SQL = """
    INSERT INTO supplement_booking_lifecycle_stage (
        tenant_key, reservation_id, order_item_id, stay_date,
        reservation_created_date, reservation_cancelled_date,
        item_created_date, item_cancelled_date,
        enterprise_id, hotel_name,
        requested_category_id, requested_category_name,
        space_category_id, space_category_name,
        amount_currency, gross_revenue
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

INVENTORY_STAGE_INSERT_SQL = """
    INSERT INTO supplement_inventory_source_stage (
        snapshot_date, tenant_key, enterprise_id, hotel_name,
        category_id, category_name, physical_inventory,
        sellable_inventory, inventory_quality
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def add_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def _booking_stage_row(row):
    return (
        row["tenant_key"], row["reservation_id"], row["order_item_id"],
        row["stay_date"], row["reservation_created_date"],
        row["reservation_cancelled_date"], row["item_created_date"],
        row["item_cancelled_date"], row["enterprise_id"],
        (row["hotel_name"] or "").strip(), row["requested_category_id"],
        (row["requested_category_name"] or "").strip(),
        row["space_category_id"], (row["space_category_name"] or "").strip(),
        row["amount_currency"], row["gross_revenue"],
    )


def _inventory_stage_row(row):
    return (
        row["snapshot_date"], row["tenant_key"], row["enterprise_id"],
        (row["hotel_name"] or "").strip(), row["category_id"],
        (row["category_name"] or "").strip(), row["physical_inventory"],
        row["sellable_inventory"], row["inventory_quality"],
    )


def _create_stages(cursor):
    cursor.execute("""
        CREATE TEMP TABLE supplement_booking_lifecycle_stage (
            tenant_key text NOT NULL,
            reservation_id uuid NOT NULL,
            order_item_id uuid NOT NULL,
            stay_date date NOT NULL,
            reservation_created_date date NOT NULL,
            reservation_cancelled_date date,
            item_created_date date NOT NULL,
            item_cancelled_date date,
            enterprise_id uuid NOT NULL,
            hotel_name text NOT NULL,
            requested_category_id uuid,
            requested_category_name text,
            space_category_id uuid,
            space_category_name text,
            amount_currency text,
            gross_revenue numeric,
            PRIMARY KEY (tenant_key, order_item_id)
        ) ON COMMIT DROP
    """)
    cursor.execute("""
        CREATE TEMP TABLE supplement_inventory_source_stage (
            snapshot_date date NOT NULL,
            tenant_key text NOT NULL,
            enterprise_id uuid NOT NULL,
            hotel_name text NOT NULL,
            category_id uuid NOT NULL,
            category_name text NOT NULL,
            physical_inventory numeric NOT NULL,
            sellable_inventory numeric NOT NULL,
            inventory_quality text NOT NULL,
            PRIMARY KEY (snapshot_date, tenant_key, enterprise_id, category_id)
        ) ON COMMIT DROP
    """)


def _materialize_snapshot_facts(cursor, source_snapshots):
    cursor.execute("""
        CREATE TEMP TABLE supplement_snapshot_stage ON COMMIT DROP AS
        WITH requested_snapshots AS (
            SELECT unnest(%s::date[]) AS snapshot_date
        ),
        eligible_items AS (
            SELECT s.snapshot_date, b.*
            FROM requested_snapshots s
            JOIN supplement_booking_lifecycle_stage b
              ON b.stay_date BETWEEN (s.snapshot_date - 7)
                                     AND (s.snapshot_date + interval '18 months')::date
             AND b.reservation_created_date <= s.snapshot_date
             AND (b.reservation_cancelled_date IS NULL
                  OR b.reservation_cancelled_date > s.snapshot_date)
             AND b.item_created_date <= s.snapshot_date
             AND (b.item_cancelled_date IS NULL
                  OR b.item_cancelled_date > s.snapshot_date)
        ),
        reservation_nights AS (
            SELECT snapshot_date, stay_date, tenant_key, reservation_id,
                   enterprise_id, hotel_name,
                   space_category_id, space_category_name,
                   requested_category_id, requested_category_name,
                   1::numeric AS assigned_rooms,
                   sum(gross_revenue)::numeric AS room_revenue
            FROM eligible_items
            GROUP BY snapshot_date, stay_date, tenant_key, reservation_id,
                     enterprise_id, hotel_name,
                     space_category_id, space_category_name,
                     requested_category_id, requested_category_name
        )
        SELECT snapshot_date, stay_date, tenant_key, enterprise_id,
               enterprise_id::text AS hotel_code, hotel_name,
               space_category_id, space_category_name,
               requested_category_id, requested_category_name,
               sum(assigned_rooms)::numeric AS assigned_rooms,
               sum(room_revenue)::numeric AS room_revenue
        FROM reservation_nights
        GROUP BY snapshot_date, stay_date, tenant_key, enterprise_id,
                 hotel_name, space_category_id, space_category_name,
                 requested_category_id, requested_category_name
    """, (source_snapshots,))


def _validate_stages(cursor, source_snapshots):
    cursor.execute("""
        SELECT count(*) AS invalid_count
        FROM supplement_booking_lifecycle_stage
        WHERE nullif(trim(tenant_key), '') IS NULL
           OR nullif(trim(hotel_name), '') IS NULL
           OR requested_category_id IS NULL
           OR space_category_id IS NULL
           OR nullif(trim(requested_category_name), '') IS NULL
           OR nullif(trim(space_category_name), '') IS NULL
           OR amount_currency IS DISTINCT FROM 'SEK'
           OR gross_revenue IS NULL OR gross_revenue < 0
    """)
    invalid_bookings = cursor.fetchone()["invalid_count"]
    if invalid_bookings:
        raise ValueError(
            f"Supplement booking source contains {invalid_bookings} invalid rows"
        )

    cursor.execute("""
        SELECT count(*) AS invalid_count
        FROM supplement_inventory_source_stage
        WHERE nullif(trim(tenant_key), '') IS NULL
           OR nullif(trim(hotel_name), '') IS NULL
           OR nullif(trim(category_name), '') IS NULL
           OR physical_inventory < 0 OR sellable_inventory < 0
           OR sellable_inventory > physical_inventory
           OR inventory_quality NOT IN ('exact', 'approximated-current')
    """)
    invalid_inventory = cursor.fetchone()["invalid_count"]
    if invalid_inventory:
        raise ValueError(
            f"Supplement inventory source contains {invalid_inventory} invalid rows"
        )

    cursor.execute("""
        SELECT count(*) AS invalid_count
        FROM supplement_booking_lifecycle_stage b
        WHERE NOT EXISTS (
            SELECT 1
            FROM supplement_inventory_source_stage i
            WHERE i.tenant_key = b.tenant_key
              AND i.enterprise_id = b.enterprise_id
              AND i.category_id = b.space_category_id
        )
    """)
    missing_inventory_mapping = cursor.fetchone()["invalid_count"]
    if missing_inventory_mapping:
        raise ValueError(
            "Supplement bookings contain "
            f"{missing_inventory_mapping} effective room-category mappings "
            "that are absent from inventory"
        )

    cursor.execute(
        "SELECT DISTINCT snapshot_date FROM supplement_inventory_source_stage"
    )
    inventory_snapshots = {row["snapshot_date"] for row in cursor.fetchall()}
    missing = sorted(set(source_snapshots) - inventory_snapshots)
    if missing:
        raise ValueError(
            "Supplement inventory is missing snapshots: "
            + ", ".join(value.isoformat() for value in missing)
        )

    cursor.execute("SELECT count(*) AS row_count FROM supplement_snapshot_stage")
    fact_count = cursor.fetchone()["row_count"]
    cursor.execute("SELECT count(*) AS row_count FROM supplement_inventory_source_stage")
    inventory_count = cursor.fetchone()["row_count"]
    cursor.execute("""
        WITH staged AS (
            SELECT snapshot_date, count(*) AS row_count
            FROM supplement_inventory_source_stage
            GROUP BY snapshot_date
        ),
        previous AS (
            SELECT snapshot_date,
                   count(DISTINCT (hotel_code, space_room_category_id)) AS row_count
            FROM functions.supplement_snapshot_inventory
            WHERE snapshot_date = ANY(%s)
            GROUP BY snapshot_date
        )
        SELECT staged.snapshot_date,
               previous.row_count AS previous_count,
               staged.row_count AS staged_count
        FROM staged
        JOIN previous USING (snapshot_date)
        ORDER BY staged.snapshot_date
    """, (source_snapshots,))
    for comparison in cursor.fetchall():
        previous_count = comparison["previous_count"]
        staged_count = comparison["staged_count"]
        if _inventory_variance_exceeds(previous_count, staged_count):
            raise ValueError(
                "Supplement inventory row-count variance exceeds 50% for "
                f"{comparison['snapshot_date']} "
                f"({previous_count} previous, {staged_count} staged)"
            )
    return fact_count, inventory_count


def _ensure_partitions(cursor, source_snapshots):
    months = {
        date(value.year, value.month, 1)
        for value in source_snapshots
    }
    for month in sorted(months):
        cursor.execute(
            "SELECT functions.ensure_supplement_month_partitions(%s)", (month,)
        )


def _publish_stage(cursor, run_id, source_snapshots):
    _ensure_partitions(cursor, source_snapshots)
    parameters = {"snapshot_dates": source_snapshots}
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
        INSERT INTO functions.hotels (
            enterprise_id, tenant_key, hotel_name, active, last_seen_at
        )
        SELECT DISTINCT ON (source.enterprise_id)
               source.enterprise_id::text, source.tenant_key,
               source.hotel_name, true, now()
        FROM supplement_inventory_source_stage AS source
        ORDER BY source.enterprise_id, source.snapshot_date DESC
        ON CONFLICT (enterprise_id) DO UPDATE SET
            tenant_key = EXCLUDED.tenant_key,
            hotel_name = EXCLUDED.hotel_name,
            active = true,
            last_seen_at = now(),
            last_updated_at = CASE
                WHEN functions.hotels.hotel_name IS DISTINCT FROM EXCLUDED.hotel_name
                THEN now() ELSE functions.hotels.last_updated_at
            END
    """)
    cursor.execute("""
        INSERT INTO functions.supplement_room_categories (
            hotel_code, room_category_id, space_room_name,
            short_name, sort_order, last_seen_at
        )
        SELECT DISTINCT ON (source.enterprise_id, source.category_id)
               source.enterprise_id::text, source.category_id,
               source.category_name, left(upper(source.category_name), 8),
               0, now()
        FROM supplement_inventory_source_stage AS source
        ORDER BY source.enterprise_id, source.category_id,
                 source.snapshot_date DESC
        ON CONFLICT (hotel_code, room_category_id) DO UPDATE SET
            space_room_name = EXCLUDED.space_room_name,
            short_name = EXCLUDED.short_name,
            last_seen_at = now()
    """)

    cursor.execute("""
        INSERT INTO functions.supplement_snapshot_detail (
            snapshot_date, stay_date, hotel_code,
            space_room_category_id, space_room_name,
            requested_room_category_id, requested_room_name,
            assigned_rooms, room_revenue, currency, run_id
        )
        SELECT snapshot_date, stay_date, hotel_code,
               space_category_id, space_category_name,
               requested_category_id, requested_category_name,
               assigned_rooms, room_revenue, 'SEK', %s
        FROM supplement_snapshot_stage
    """, (run_id,))
    cursor.execute("""
        INSERT INTO functions.supplement_snapshot_category (
            snapshot_date, stay_date, hotel_code,
            space_room_category_id, space_room_name,
            assigned_rooms, room_revenue, currency, run_id
        )
        SELECT snapshot_date, stay_date, hotel_code,
               space_category_id, max(space_category_name),
               sum(assigned_rooms), sum(room_revenue), 'SEK', %s
        FROM supplement_snapshot_stage
        GROUP BY snapshot_date, stay_date, hotel_code, space_category_id
    """, (run_id,))
    cursor.execute("""
        INSERT INTO functions.supplement_snapshot_inventory (
            snapshot_date, stay_date, hotel_code,
            space_room_category_id, space_room_name,
            total_space, space_to_sell, inventory_quality, run_id
        )
        SELECT i.snapshot_date, stay.day::date, i.enterprise_id::text,
               i.category_id, i.category_name,
               i.physical_inventory, i.sellable_inventory,
               i.inventory_quality, %s
        FROM supplement_inventory_source_stage i
        CROSS JOIN LATERAL generate_series(
            i.snapshot_date - 7,
            (i.snapshot_date + interval '18 months')::date,
            interval '1 day'
        ) AS stay(day)
    """, (run_id,))

    cursor.execute("""
        SELECT min(stay_date) AS minimum_stay_date,
               max(stay_date) AS maximum_stay_date
        FROM functions.supplement_snapshot_inventory
        WHERE snapshot_date = ANY(%s)
    """, (source_snapshots,))
    staged_range = cursor.fetchone()
    rebuild = {
        "minimum_stay_date": staged_range["minimum_stay_date"],
        "maximum_stay_date": staged_range["maximum_stay_date"],
    }
    for table in (
        "supplement_latest_detail",
        "supplement_latest_category",
        "supplement_latest_inventory",
    ):
        cursor.execute(
            f"DELETE FROM functions.{table} "
            "WHERE stay_date BETWEEN %(minimum_stay_date)s AND %(maximum_stay_date)s",
            rebuild,
        )

    cursor.execute("""
        INSERT INTO functions.supplement_latest_inventory (
            stay_date, hotel_code, space_room_category_id, space_room_name,
            snapshot_date, total_space, space_to_sell,
            inventory_quality, run_id
        )
        WITH chosen AS (
            SELECT stay_date, hotel_code, max(snapshot_date) AS snapshot_date
            FROM functions.supplement_snapshot_inventory
            WHERE stay_date BETWEEN %(minimum_stay_date)s AND %(maximum_stay_date)s
            GROUP BY stay_date, hotel_code
        )
        SELECT i.stay_date, i.hotel_code, i.space_room_category_id,
               i.space_room_name, i.snapshot_date, i.total_space,
               i.space_to_sell, i.inventory_quality, i.run_id
        FROM functions.supplement_snapshot_inventory i
        JOIN chosen c USING (stay_date, hotel_code, snapshot_date)
    """, rebuild)
    cursor.execute("""
        INSERT INTO functions.supplement_latest_category (
            stay_date, hotel_code, space_room_category_id, space_room_name,
            snapshot_date, assigned_rooms, room_revenue, currency, run_id
        )
        SELECT c.stay_date, c.hotel_code, c.space_room_category_id,
               c.space_room_name, c.snapshot_date, c.assigned_rooms,
               c.room_revenue, c.currency, c.run_id
        FROM functions.supplement_snapshot_category c
        JOIN functions.supplement_latest_inventory i
          USING (stay_date, hotel_code, space_room_category_id, snapshot_date)
        WHERE c.stay_date BETWEEN %(minimum_stay_date)s AND %(maximum_stay_date)s
    """, rebuild)
    cursor.execute("""
        INSERT INTO functions.supplement_latest_detail (
            stay_date, hotel_code, space_room_category_id, space_room_name,
            requested_room_category_id, requested_room_name,
            snapshot_date, assigned_rooms, room_revenue, currency, run_id
        )
        SELECT d.stay_date, d.hotel_code, d.space_room_category_id,
               d.space_room_name, d.requested_room_category_id,
               d.requested_room_name, d.snapshot_date, d.assigned_rooms,
               d.room_revenue, d.currency, d.run_id
        FROM functions.supplement_snapshot_detail d
        JOIN functions.supplement_latest_inventory i
          USING (stay_date, hotel_code, space_room_category_id, snapshot_date)
        WHERE d.stay_date BETWEEN %(minimum_stay_date)s AND %(maximum_stay_date)s
    """, rebuild)

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
    # Dense pickup snapshots expire; supplement_latest_* remains the permanent
    # latest/final fact set and is deliberately never deleted here.
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
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired",
                (SYNC_LOCK_NAME,),
            )
            if not cursor.fetchone()["acquired"]:
                raise RuntimeError("Another Supplement synchronization is already running")

            run_id = None
            try:
                latest_source = stockholm_today()
                published = _published_snapshot(cursor)
                if mode == "delta":
                    snapshot_to = latest_source
                    correction_start = latest_source - timedelta(
                        days=SOURCE_OVERLAP_DAYS - 1
                    )
                    snapshot_from = correction_start
                else:
                    if snapshot_from is None or snapshot_to is None:
                        raise ValueError("repair and backfill require snapshotFrom and snapshotTo")
                    if snapshot_from > snapshot_to:
                        raise ValueError("snapshotFrom cannot be after snapshotTo")
                    if snapshot_to > latest_source:
                        raise ValueError("snapshotTo cannot be in the future")

                source_snapshots = snapshot_dates(snapshot_from, snapshot_to)
                cursor.execute("""
                    INSERT INTO functions.supplement_sync_runs (
                        mode, status, source_snapshot_from, source_snapshot_to
                    ) VALUES (%s, 'running', %s, %s)
                    RETURNING run_id
                """, (mode, snapshot_from, snapshot_to))
                run_id = cursor.fetchone()["run_id"]
                app_connection.commit()

                _create_stages(cursor)
                minimum_stay_date = min(source_snapshots) - timedelta(days=7)
                maximum_stay_date = max(
                    add_months(value, 18) for value in source_snapshots
                )
                booking_rows = 0
                for rows in iter_booking_lifecycle_batches(
                    source_snapshots,
                    minimum_stay_date,
                    maximum_stay_date,
                    BATCH_SIZE,
                ):
                    cursor.executemany(
                        BOOKING_STAGE_INSERT_SQL,
                        [_booking_stage_row(row) for row in rows],
                    )
                    booking_rows += len(rows)

                inventory_rows = 0
                for rows in iter_inventory_batches(source_snapshots, BATCH_SIZE):
                    cursor.executemany(
                        INVENTORY_STAGE_INSERT_SQL,
                        [_inventory_stage_row(row) for row in rows],
                    )
                    inventory_rows += len(rows)

                _materialize_snapshot_facts(cursor, source_snapshots)
                fact_rows, staged_inventory_rows = _validate_stages(
                    cursor, source_snapshots
                )
                _publish_stage(cursor, run_id, source_snapshots)
                publication_date = max(published or snapshot_to, snapshot_to)
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
                exported_rows = booking_rows + inventory_rows
                imported_rows = fact_rows + staged_inventory_rows
                cursor.execute("""
                    UPDATE functions.supplement_sync_runs
                    SET status = 'published', exported_rows = %s,
                        imported_rows = %s, finished_at = now(), published_at = now()
                    WHERE run_id = %s
                """, (exported_rows, imported_rows, run_id))
                app_connection.commit()
                logging.info(
                    "Supplement lifecycle sync published run_id=%s mode=%s "
                    "snapshots=%s..%s booking_rows=%s inventory_rows=%s "
                    "fact_rows=%s elapsed_seconds=%.2f",
                    run_id, mode, snapshot_from, snapshot_to, booking_rows,
                    inventory_rows, fact_rows, perf_counter() - started_at,
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
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))", (SYNC_LOCK_NAME,)
                )


def run_backfill_partition(snapshot_date):
    return sync_supplement("backfill", snapshot_date, snapshot_date)
