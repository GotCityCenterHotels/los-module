import logging
import os

from datetime import timedelta
from time import monotonic

from psycopg.rows import dict_row

from cost_database import apply_background_timeouts, cost_pool
from services.cost_publication_service import (
    advance_cost_publication,
    remember_cost_publication,
)
from queries.los_sync import (
    AFFECTED_RESERVATIONS_SQL,
    FILTERED_FACT_SOURCE_SQL,
    FILTERED_IDENTITY_SOURCE_SQL,
    FULL_FACT_SOURCE_SQL,
    FULL_IDENTITY_SOURCE_SQL,
    SOURCE_CLOCK_SQL,
)
from services.los_schema_service import ensure_los_schema
from shared.db import get_export_connection, get_import_connection


SYNC_LOCK_NAME = "functions.los_sync"
# The read path's index-only scan is only index-only where the visibility map
# says so, and a bulk publication plus the pruning below leaves that map unset
# until autovacuum happens to come round - which after a nightly write burst can
# be hours after the rows anyone reads were written. Vacuuming here ties it to
# the publication instead of to a daemon's schedule.
VACUUM_TABLES = (
    "functions.reservation_los_daily",
    "functions.reservation_los_fact",
)
WATERMARK_OVERLAP_MINUTES = int(
    os.environ.get("LOS_WATERMARK_OVERLAP_MINUTES", "5")
)
EXTRACT_BATCH_SIZE = int(os.environ.get("LOS_EXTRACT_BATCH_SIZE", "5000"))
RESERVATION_CHUNK_SIZE = int(
    os.environ.get("LOS_RESERVATION_CHUNK_SIZE", "5000")
)

# How many superseded publications to keep alongside the live one.
#
# It was seven, so reservation_los_daily carried eight generations of rows and
# ix_reservation_los_daily_lookup indexed all of them. The read path filters on a
# single run_id, so seven eighths of that index was dead weight it still had to
# descend through. One previous generation is what a rollback actually needs: the
# publication pointer can be moved back to it without re-running a sync.
LOS_RUN_RETENTION = max(0, int(os.environ.get("LOS_RUN_RETENTION", "1")))


AGGREGATE_SQL = """
INSERT INTO functions.reservation_los_daily (
    run_id, comparison_basis, arrival_date, enterprise_id,
    scenario, los, booking_count, night_count
)
WITH basis AS (
    SELECT
        b.comparison_basis,
        CASE WHEN b.comparison_basis = 'sameWeekday'
            THEN %(as_of_date)s::date - 364
            ELSE (%(as_of_date)s::date - INTERVAL '1 year')::date
        END AS spit_cutoff
    FROM (VALUES ('sameDate'::text), ('sameWeekday'::text)) b(comparison_basis)
),
current_aggregate AS (
    SELECT
        basis.comparison_basis,
        fact.arrival_date,
        fact.enterprise_id,
        'current'::text AS scenario,
        fact.los,
        count(*)::bigint AS booking_count
    FROM functions.reservation_los_fact fact
    CROSS JOIN basis
    WHERE fact.fact_kind = 'current'
    GROUP BY
        basis.comparison_basis, fact.arrival_date,
        fact.enterprise_id, fact.los
),
ly_aggregate AS (
    -- Last year's final state. historical facts are stored one row per distinct
    -- cancellation date, so the single cancelled_date IS NULL row per
    -- reservation already carries the surviving length of stay.
    SELECT
        basis.comparison_basis,
        CASE WHEN basis.comparison_basis = 'sameWeekday'
            THEN fact.arrival_date + 364
            ELSE (fact.arrival_date + INTERVAL '1 year')::date
        END AS arrival_date,
        fact.enterprise_id,
        'ly'::text AS scenario,
        fact.los,
        count(*)::bigint AS booking_count
    FROM functions.reservation_los_fact fact
    CROSS JOIN basis
    WHERE fact.fact_kind = 'historical'
      AND fact.cancelled_date IS NULL
    GROUP BY 1, 2, 3, 5
),
spit_reservation AS (
    -- Same point in time: rebuild each reservation as it stood at the cutoff.
    --
    -- A reservation shortened after the cutoff is stored as several rows (the
    -- surviving nights, plus one row per cancellation date). Counting those
    -- rows treated one booking as several and split its length of stay across
    -- them, so a 3-night stay reported as a 2-night plus a 1-night booking.
    -- Collapsing back to one row per reservation first restores both the
    -- booking count and the length-of-stay distribution.
    SELECT
        basis.comparison_basis,
        CASE WHEN basis.comparison_basis = 'sameWeekday'
            THEN fact.arrival_date + 364
            ELSE (fact.arrival_date + INTERVAL '1 year')::date
        END AS arrival_date,
        fact.enterprise_id,
        fact.reservation_number,
        sum(fact.los)::int AS los
    FROM functions.reservation_los_fact fact
    CROSS JOIN basis
    WHERE fact.fact_kind = 'historical'
      AND fact.created_date <= basis.spit_cutoff
      AND (
          fact.cancelled_date IS NULL
          OR fact.cancelled_date > basis.spit_cutoff
      )
    GROUP BY 1, 2, 3, 4
),
spit_aggregate AS (
    SELECT
        comparison_basis,
        arrival_date,
        enterprise_id,
        'spit'::text AS scenario,
        los,
        count(*)::bigint AS booking_count
    FROM spit_reservation
    GROUP BY 1, 2, 3, 5
),
combined AS (
    SELECT * FROM current_aggregate
    UNION ALL
    SELECT * FROM ly_aggregate
    UNION ALL
    SELECT * FROM spit_aggregate
)
SELECT
    %(run_id)s, comparison_basis, arrival_date, enterprise_id,
    scenario, los, booking_count,
    (los::bigint * booking_count)::bigint
FROM combined
WHERE booking_count > 0
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
        # Named cursors stream from integration_db instead of buffering the whole
        # result set into worker memory. One cursor per query: a server-side
        # cursor is bound to the statement it declares.
        for index, (source_sql, chunk) in enumerate(source_queries):
            parameters = (
                None if chunk is None else {"reservation_numbers": chunk}
            )
            with source_connection.cursor(name=f"los_identity_{index}") as source_cursor:
                source_cursor.itersize = EXTRACT_BATCH_SIZE
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
        with source_connection.cursor() as setup_cursor:
            setup_cursor.execute("SET LOCAL work_mem = '64MB'")
        # Named cursors stream from integration_db instead of buffering the whole
        # fact set into worker memory - which on a "full" sync is all history.
        for index, (source_sql, chunk) in enumerate(source_queries):
            parameters = (
                None if chunk is None else {"reservation_numbers": chunk}
            )
            with source_connection.cursor(name=f"los_facts_{index}") as source_cursor:
                source_cursor.itersize = EXTRACT_BATCH_SIZE
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


def _vacuum_read_model():
    """Set the visibility map and refresh statistics after a publication.

    Deliberately best-effort. The publication is already committed and correct
    by the time this runs, so a vacuum that is blocked, slow, or refused is a
    lost optimisation rather than a failed sync, and must not turn a good
    publication into a reported failure.

    On its own connection, in autocommit: VACUUM is not allowed inside a
    transaction block, and flipping autocommit on a pooled connection would
    carry that change back into the pool.
    """
    for table in VACUUM_TABLES:
        try:
            with get_import_connection() as connection:
                connection.autocommit = True
                with connection.cursor() as cursor:
                    cursor.execute(f"VACUUM (ANALYZE) {table}")
            logging.info("LOS read model vacuumed table=%s", table)
        except Exception:
            logging.warning(
                "LOS read model vacuum skipped table=%s", table, exc_info=True
            )


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
                    apply_background_timeouts(cursor)
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
                            OFFSET %s
                        )
                    """, (run_id, LOS_RUN_RETENTION))
                    cost_publication_version = None
                    if fact_rows_written:
                        cost_publication_version = advance_cost_publication(
                            "hotels:los-sync",
                            cursor=cursor,
                        )

            # Outside the transaction on purpose: VACUUM cannot run inside one.
            _vacuum_read_model()

            if cost_publication_version is not None:
                remember_cost_publication(cost_publication_version)

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
