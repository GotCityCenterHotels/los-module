import logging
import os

from datetime import timedelta
from time import monotonic

from psycopg.rows import dict_row

from cost_database import cost_pool
from queries.los_sync import (
    AFFECTED_RESERVATIONS_SQL,
    FILTERED_FACT_SOURCE_SQL,
    FILTERED_IDENTITY_SOURCE_SQL,
    FULL_FACT_SOURCE_SQL,
    FULL_IDENTITY_SOURCE_SQL,
    SOURCE_CLOCK_SQL,
)
from services.los_schema_service import ensure_los_schema
from shared.db import get_export_connection


SYNC_LOCK_NAME = "functions.los_sync"
WATERMARK_OVERLAP_MINUTES = int(
    os.environ.get("LOS_WATERMARK_OVERLAP_MINUTES", "5")
)
EXTRACT_BATCH_SIZE = int(os.environ.get("LOS_EXTRACT_BATCH_SIZE", "5000"))
RESERVATION_CHUNK_SIZE = int(
    os.environ.get("LOS_RESERVATION_CHUNK_SIZE", "5000")
)


AGGREGATE_SQL = """
INSERT INTO functions.reservation_los_daily (
    run_id, comparison_basis, arrival_date, enterprise_id,
    scenario, los, booking_count, night_count
)
WITH current_aggregate AS (
    SELECT
        basis.comparison_basis,
        fact.arrival_date,
        fact.enterprise_id,
        'current'::text AS scenario,
        fact.los,
        count(*)::bigint AS booking_count
    FROM functions.reservation_los_fact fact
    CROSS JOIN (VALUES ('sameDate'::text), ('sameWeekday'::text))
        basis(comparison_basis)
    WHERE fact.fact_kind = 'current'
    GROUP BY
        basis.comparison_basis, fact.arrival_date,
        fact.enterprise_id, fact.los
),
historical_components AS (
    SELECT
        basis.comparison_basis,
        CASE WHEN basis.comparison_basis = 'sameWeekday'
            THEN fact.arrival_date + 364
            ELSE (fact.arrival_date + INTERVAL '1 year')::date
        END AS arrival_date,
        fact.enterprise_id,
        fact.los,
        count(*) FILTER (WHERE fact.cancelled_date IS NULL)::bigint
            AS ly_booking_count,
        count(*) FILTER (
            WHERE fact.created_date <= (
                CASE WHEN basis.comparison_basis = 'sameWeekday'
                    THEN %(as_of_date)s::date - 364
                    ELSE (%(as_of_date)s::date - INTERVAL '1 year')::date
                END
            )
              AND (
                  fact.cancelled_date IS NULL
                  OR fact.cancelled_date > (
                      CASE WHEN basis.comparison_basis = 'sameWeekday'
                          THEN %(as_of_date)s::date - 364
                          ELSE (%(as_of_date)s::date - INTERVAL '1 year')::date
                      END
                  )
              )
        )::bigint AS spit_booking_count
    FROM functions.reservation_los_fact fact
    CROSS JOIN (VALUES ('sameDate'::text), ('sameWeekday'::text))
        basis(comparison_basis)
    WHERE fact.fact_kind = 'historical'
    GROUP BY
        basis.comparison_basis,
        CASE WHEN basis.comparison_basis = 'sameWeekday'
            THEN fact.arrival_date + 364
            ELSE (fact.arrival_date + INTERVAL '1 year')::date
        END,
        fact.enterprise_id,
        fact.los
),
historical_aggregate AS (
    SELECT
        component.comparison_basis,
        component.arrival_date,
        component.enterprise_id,
        scenario.scenario,
        component.los,
        scenario.booking_count
    FROM historical_components component
    CROSS JOIN LATERAL (
        VALUES
            ('ly'::text, component.ly_booking_count),
            ('spit'::text, component.spit_booking_count)
    ) scenario(scenario, booking_count)
    WHERE scenario.booking_count > 0
),
combined AS (
    SELECT * FROM current_aggregate
    UNION ALL
    SELECT * FROM historical_aggregate
)
SELECT
    %(run_id)s, comparison_basis, arrival_date, enterprise_id,
    scenario, los, booking_count,
    (los::bigint * booking_count)::bigint
FROM combined
"""


UPSERT_HOTELS_SQL = """
INSERT INTO functions.hotels (
    enterprise_id, tenant_key, hotel_name, active, last_seen_at
)
VALUES (%s, 'GCCH', %s, true, now())
ON CONFLICT (enterprise_id) DO UPDATE SET
    tenant_key = EXCLUDED.tenant_key,
    hotel_name = EXCLUDED.hotel_name,
    active = true,
    last_seen_at = now(),
    last_updated_at = CASE
        WHEN functions.hotels.hotel_name IS DISTINCT FROM EXCLUDED.hotel_name
        THEN now() ELSE functions.hotels.last_updated_at
    END
"""


UPSERT_FACTS_SQL = """
INSERT INTO functions.reservation_los_fact (
    fact_key, fact_kind, reservation_number, enterprise_id,
    arrival_date, created_date, cancelled_date, los, source_updated_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (fact_key) DO UPDATE SET
    fact_kind = EXCLUDED.fact_kind,
    reservation_number = EXCLUDED.reservation_number,
    enterprise_id = EXCLUDED.enterprise_id,
    arrival_date = EXCLUDED.arrival_date,
    created_date = EXCLUDED.created_date,
    cancelled_date = EXCLUDED.cancelled_date,
    los = EXCLUDED.los,
    source_updated_at = EXCLUDED.source_updated_at,
    last_updated_at = CASE
        WHEN (
            functions.reservation_los_fact.enterprise_id,
            functions.reservation_los_fact.arrival_date,
            functions.reservation_los_fact.created_date,
            functions.reservation_los_fact.cancelled_date,
            functions.reservation_los_fact.los,
            functions.reservation_los_fact.source_updated_at
        ) IS DISTINCT FROM (
            EXCLUDED.enterprise_id,
            EXCLUDED.arrival_date,
            EXCLUDED.created_date,
            EXCLUDED.cancelled_date,
            EXCLUDED.los,
            EXCLUDED.source_updated_at
        ) THEN now()
        ELSE functions.reservation_los_fact.last_updated_at
    END
"""


UPSERT_IDENTITIES_SQL = """
INSERT INTO functions.los_reservation_identity (
    reservation_id, reservation_number, source_updated_at
)
VALUES (%s, %s, %s)
ON CONFLICT (reservation_id) DO UPDATE SET
    reservation_number = EXCLUDED.reservation_number,
    source_updated_at = EXCLUDED.source_updated_at,
    last_updated_at = CASE
        WHEN (
            functions.los_reservation_identity.reservation_number,
            functions.los_reservation_identity.source_updated_at
        ) IS DISTINCT FROM (
            EXCLUDED.reservation_number,
            EXCLUDED.source_updated_at
        ) THEN now()
        ELSE functions.los_reservation_identity.last_updated_at
    END
"""


def _chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _source_clock():
    with get_export_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(SOURCE_CLOCK_SQL)
            return cursor.fetchone()


def _previous_watermark():
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT run.source_watermark_to
                FROM functions.los_publication publication
                JOIN functions.los_sync_runs run USING (run_id)
                WHERE publication.singleton
            """)
            row = cursor.fetchone()
            return row[0] if row else None


def _affected_reservations(watermark_from, watermark_to):
    with get_export_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                AFFECTED_RESERVATIONS_SQL,
                {
                    "watermark_from": watermark_from,
                    "watermark_to": watermark_to,
                },
            )
            return cursor.fetchall()


def _write_identity_batch(cursor, rows):
    if not rows:
        return 0
    cursor.executemany(
        UPSERT_IDENTITIES_SQL,
        [
            (
                row["reservation_id"],
                row["reservation_number"],
                row["source_updated_at"],
            )
            for row in rows
        ],
    )
    return len(rows)


def _extract_and_write_identities(target_cursor, reservation_numbers):
    written = 0
    source_queries = (
        [(FULL_IDENTITY_SOURCE_SQL, None)]
        if reservation_numbers is None
        else [
            (FILTERED_IDENTITY_SOURCE_SQL, chunk)
            for chunk in _chunks(reservation_numbers, RESERVATION_CHUNK_SIZE)
        ]
    )
    with get_export_connection() as source_connection:
        with source_connection.cursor() as source_cursor:
            for source_sql, chunk in source_queries:
                parameters = (
                    None if chunk is None else {"reservation_numbers": chunk}
                )
                source_cursor.execute(source_sql, parameters)
                while True:
                    rows = source_cursor.fetchmany(EXTRACT_BATCH_SIZE)
                    if not rows:
                        break
                    written += _write_identity_batch(target_cursor, rows)
    return written


def _write_fact_batch(cursor, rows):
    if not rows:
        return 0
    hotels = sorted({
        (row["enterprise_id"], row["hotel_name"])
        for row in rows
    })
    cursor.executemany(UPSERT_HOTELS_SQL, hotels)
    cursor.executemany(
        UPSERT_FACTS_SQL,
        [
            (
                row["fact_key"],
                row["fact_kind"],
                row["reservation_number"],
                row["enterprise_id"],
                row["arrival_date"],
                row["created_date"],
                row["cancelled_date"],
                row["los"],
                row["source_updated_at"],
            )
            for row in rows
        ],
    )
    return len(rows)


def _extract_and_write_facts(target_cursor, reservation_numbers):
    exported = 0
    source_queries = (
        [(FULL_FACT_SOURCE_SQL, None)]
        if reservation_numbers is None
        else [
            (FILTERED_FACT_SOURCE_SQL, chunk)
            for chunk in _chunks(reservation_numbers, RESERVATION_CHUNK_SIZE)
        ]
    )
    with get_export_connection() as source_connection:
        with source_connection.cursor() as source_cursor:
            source_cursor.execute("SET LOCAL work_mem = '64MB'")
            for source_sql, chunk in source_queries:
                parameters = (
                    None if chunk is None else {"reservation_numbers": chunk}
                )
                source_cursor.execute(source_sql, parameters)
                while True:
                    rows = source_cursor.fetchmany(EXTRACT_BATCH_SIZE)
                    if not rows:
                        break
                    exported += _write_fact_batch(target_cursor, rows)
    return exported


def _create_run(mode, watermark_from, source_clock):
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO functions.los_sync_runs (
                    mode, status, source_watermark_from,
                    source_watermark_to, source_as_of_date
                ) VALUES (%s, 'running', %s, %s, %s)
                RETURNING run_id
            """, (
                mode,
                watermark_from,
                source_clock["upper_watermark"],
                source_clock["as_of_date"],
            ))
            return cursor.fetchone()[0]


def _mark_failed(run_id, error):
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE functions.los_sync_runs
                SET status = 'failed', finished_at = now(),
                    error_message = %s
                WHERE run_id = %s AND status = 'running'
            """, (str(error).splitlines()[0][:2000], run_id))


def sync_los(mode="delta"):
    if mode not in {"delta", "full"}:
        raise ValueError("LOS synchronization mode must be delta or full")
    ensure_los_schema()

    with cost_pool.connection() as lock_connection:
        with lock_connection.cursor() as lock_cursor:
            lock_cursor.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                (SYNC_LOCK_NAME,),
            )
            if not lock_cursor.fetchone()[0]:
                raise RuntimeError("A LOS synchronization is already running")

        run_id = None
        started = monotonic()
        try:
            source_clock = _source_clock()
            previous = _previous_watermark()
            effective_mode = "full" if mode == "full" or previous is None else "delta"
            watermark_from = (
                previous - timedelta(minutes=WATERMARK_OVERLAP_MINUTES)
                if effective_mode == "delta"
                else None
            )
            affected = (
                _affected_reservations(
                    watermark_from, source_clock["upper_watermark"]
                )
                if effective_mode == "delta"
                else None
            )
            run_id = _create_run(effective_mode, watermark_from, source_clock)

            with cost_pool.connection() as connection:
                with connection.cursor() as cursor:
                    if effective_mode == "full":
                        cursor.execute("DELETE FROM functions.reservation_los_fact")
                        cursor.execute(
                            "DELETE FROM functions.los_reservation_identity"
                        )
                        # Do not execute the unbounded all-history fact query.
                        # It can exceed integration_db's enforced statement
                        # timeout even though date-bounded LOS reads are fast.
                        # Load the cheap reservation identity set first, then
                        # calculate facts in bounded reservation-number chunks.
                        # Chunking on number (rather than reservation id) keeps
                        # duplicate reservation numbers in the same query and
                        # therefore preserves the canonical grouping semantics.
                        identity_rows_written = _extract_and_write_identities(
                            cursor,
                            None,
                        )
                        cursor.execute("""
                            SELECT DISTINCT reservation_number
                            FROM functions.los_reservation_identity
                            WHERE reservation_number IS NOT NULL
                            ORDER BY reservation_number
                        """)
                        recalculation_numbers = [
                            row[0] for row in cursor.fetchall()
                        ]
                    elif affected:
                        affected_ids = [row["reservation_id"] for row in affected]
                        new_numbers = {
                            row["reservation_number"]
                            for row in affected
                            if row["reservation_number"]
                        }
                        old_numbers = set()
                        for chunk in _chunks(
                            affected_ids, RESERVATION_CHUNK_SIZE
                        ):
                            cursor.execute("""
                                SELECT DISTINCT reservation_number
                                FROM functions.los_reservation_identity
                                WHERE reservation_id = ANY(%s)
                            """, (chunk,))
                            old_numbers.update(row[0] for row in cursor.fetchall())
                        recalculation_numbers = sorted(old_numbers | new_numbers)
                        for chunk in _chunks(
                            recalculation_numbers, RESERVATION_CHUNK_SIZE
                        ):
                            cursor.execute(
                                "DELETE FROM functions.reservation_los_fact "
                                "WHERE reservation_number = ANY(%s)",
                                (chunk,),
                            )
                        for chunk in _chunks(
                            affected_ids, RESERVATION_CHUNK_SIZE
                        ):
                            cursor.execute(
                                "DELETE FROM functions.los_reservation_identity "
                                "WHERE reservation_id = ANY(%s)",
                                (chunk,),
                            )
                    else:
                        recalculation_numbers = []

                    fact_rows_written = _extract_and_write_facts(
                        cursor,
                        recalculation_numbers,
                    )
                    if effective_mode != "full":
                        identity_rows_written = _extract_and_write_identities(
                            cursor,
                            recalculation_numbers,
                        )
                    cursor.execute(
                        AGGREGATE_SQL,
                        {
                            "run_id": run_id,
                            "as_of_date": source_clock["as_of_date"],
                        },
                    )
                    aggregate_rows = cursor.rowcount
                    cursor.execute("""
                        SELECT count(*)
                        FROM functions.reservation_los_daily
                        WHERE run_id = %s
                          AND night_count <> los::bigint * booking_count
                    """, (run_id,))
                    if cursor.fetchone()[0] != 0:
                        raise RuntimeError("LOS aggregate validation failed")
                    cursor.execute("""
                        INSERT INTO functions.los_publication (
                            singleton, run_id, published_at
                        ) VALUES (true, %s, clock_timestamp())
                        ON CONFLICT (singleton) DO UPDATE SET
                            run_id = EXCLUDED.run_id,
                            published_at = EXCLUDED.published_at
                    """, (run_id,))
                    cursor.execute("""
                        UPDATE functions.los_sync_runs
                        SET status = 'published',
                            affected_reservations = %s,
                            fact_rows = (
                                SELECT count(*)
                                FROM functions.reservation_los_fact
                            ),
                            aggregate_rows = %s,
                            finished_at = clock_timestamp(),
                            published_at = clock_timestamp()
                        WHERE run_id = %s
                    """, (
                            len(affected) if affected is not None else identity_rows_written,
                        aggregate_rows,
                        run_id,
                    ))
                    cursor.execute("""
                        DELETE FROM functions.los_sync_runs old_run
                        WHERE old_run.run_id IN (
                            SELECT run_id
                            FROM functions.los_sync_runs
                            WHERE status IN ('published', 'failed')
                              AND run_id <> %s
                            ORDER BY run_id DESC
                            OFFSET 7
                        )
                    """, (run_id,))

            result = {
                "status": "success",
                "mode": effective_mode,
                "runId": run_id,
                "sourceAsOfDate": source_clock["as_of_date"].isoformat(),
                "affectedReservations": (
                    len(affected) if affected is not None else None
                ),
                "factRowsWritten": fact_rows_written,
                "identityRowsWritten": identity_rows_written,
                "aggregateRows": aggregate_rows,
                "durationSeconds": round(monotonic() - started, 3),
            }
            logging.info("LOS synchronization published %s", result)
            return result
        except Exception as error:
            if run_id is not None:
                _mark_failed(run_id, error)
            logging.exception("LOS synchronization failed mode=%s", mode)
            raise
        finally:
            with lock_connection.cursor() as lock_cursor:
                lock_cursor.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))",
                    (SYNC_LOCK_NAME,),
                )
