import logging
import os

from datetime import datetime, timedelta, timezone
from time import monotonic

from psycopg.rows import dict_row

from cost_database import cost_pool
from database import pool
from queries.los_facts import LOS_FACTS_SQL
from services.los_schema_service import ensure_los_schema


LOS_STALE_AFTER_HOURS = int(os.environ.get("LOS_STALE_AFTER_HOURS", "30"))


class LosReadModelUnavailableError(RuntimeError):
    pass


def los_read_model_enabled():
    return os.environ.get("LOS_READ_MODEL_ENABLED", "false").lower() in {
        "1", "true", "yes", "on"
    }


def _fact_json(row):
    return {
        "arrivalDate": row["arrival_date"].isoformat(),
        # Keep hotelCode backward-compatible for existing filters while
        # exposing the stable unified identity for new consumers.
        "hotelCode": row["hotel_name"],
        "enterpriseId": row["hotel_code"],
        "hotelName": row["hotel_name"],
        "scenario": row["scenario"],
        "los": int(row["los"]),
        "bookingCount": int(row["booking_count"]),
        "nightCount": int(row["night_count"]),
    }


def fetch_los_read_model_status():
    ensure_los_schema()
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""
                SELECT publication.run_id, publication.published_at,
                       run.source_as_of_date, run.source_watermark_to,
                       run.mode, run.fact_rows, run.aggregate_rows
                FROM functions.los_publication publication
                JOIN functions.los_sync_runs run USING (run_id)
                WHERE publication.singleton
            """)
            row = cursor.fetchone()
    if row is None:
        return {
            "status": "unavailable",
            "stale": True,
            "runId": None,
            "publishedAt": None,
        }
    stale = (
        datetime.now(timezone.utc) - row["published_at"]
        > timedelta(hours=LOS_STALE_AFTER_HOURS)
    )
    return {
        "status": "stale" if stale else "available",
        "stale": stale,
        "runId": row["run_id"],
        "publishedAt": row["published_at"].isoformat(),
        "sourceAsOfDate": row["source_as_of_date"].isoformat(),
        "sourceWatermark": row["source_watermark_to"].isoformat(),
        "mode": row["mode"],
        "factRows": row["fact_rows"],
        "aggregateRows": row["aggregate_rows"],
    }


def _fetch_published_los_facts(start_date, end_date, ly_comparison_basis):
    ensure_los_schema()
    started_at = monotonic()
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""
                SELECT daily.arrival_date,
                       daily.enterprise_id AS hotel_code,
                       hotel.hotel_name,
                       daily.scenario, daily.los,
                       daily.booking_count, daily.night_count,
                       publication.published_at
                FROM functions.los_publication publication
                JOIN functions.reservation_los_daily daily
                  ON daily.run_id = publication.run_id
                JOIN functions.hotels hotel
                  ON hotel.enterprise_id = daily.enterprise_id
                WHERE publication.singleton
                  AND daily.comparison_basis = %s
                  AND daily.arrival_date BETWEEN %s AND %s
                ORDER BY daily.arrival_date, hotel.hotel_name,
                         daily.scenario, daily.los
            """, (ly_comparison_basis, start_date, end_date))
            rows = cursor.fetchall()
    if not rows:
        status = fetch_los_read_model_status()
        if status["runId"] is None:
            raise LosReadModelUnavailableError(
                "LOS read model has not been published"
            )
        if status["stale"]:
            logging.warning(
                "LOS publication is stale run_id=%s published_at=%s",
                status["runId"],
                status["publishedAt"],
            )
    elif (
        datetime.now(timezone.utc) - rows[0]["published_at"]
        > timedelta(hours=LOS_STALE_AFTER_HOURS)
    ):
        logging.warning(
            "LOS publication is stale published_at=%s",
            rows[0]["published_at"].isoformat(),
        )
    elapsed_ms = (monotonic() - started_at) * 1000
    logging.info(
        "LOS read model query completed run_id=%s row_count=%d duration_ms=%.1f",
        status["runId"] if not rows else "published",
        len(rows),
        elapsed_ms,
    )
    if elapsed_ms >= 500:
        logging.warning(
            "LOS read model query exceeded target duration_ms=%.1f", elapsed_ms
        )
    return [_fact_json(row) for row in rows]


def fetch_los_facts(start_date, end_date, ly_comparison_basis):
    if los_read_model_enabled():
        return _fetch_published_los_facts(
            start_date, end_date, ly_comparison_basis
        )

    parameters = {
        "start_date": start_date,
        "end_date": end_date,
        "ly_comparison_basis": ly_comparison_basis,
    }

    started_at = monotonic()

    with pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SET LOCAL work_mem = '64MB'")
            cursor.execute(LOS_FACTS_SQL, parameters)
            rows = cursor.fetchall()

    logging.info(
        "LOS facts query completed start_date=%s end_date=%s "
        "ly_comparison_basis=%s row_count=%d duration_ms=%.1f",
        start_date,
        end_date,
        ly_comparison_basis,
        len(rows),
        (monotonic() - started_at) * 1000,
    )

    return [_fact_json(row) for row in rows]
