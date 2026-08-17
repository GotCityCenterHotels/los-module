import logging
import os

from collections import namedtuple
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


# run_id and published_at identify the publication the rows came from, which is
# what lets the HTTP layer put a validator on the response: the same range asked
# for twice against the same publication is the same bytes, and the browser can
# be told so. They are None on the raw-query fallback, which has no publication
# to name.
LosFacts = namedtuple("LosFacts", ("rows", "run_id", "published_at"))


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


def _is_stale(published_at):
    return (
        datetime.now(timezone.utc) - published_at
        > timedelta(hours=LOS_STALE_AFTER_HOURS)
    )


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
    stale = _is_stale(row["published_at"])
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
            # Resolving the publication first, on its own, is what lets the fact
            # query below take a constant run_id and read nothing but its own
            # index. It also fails fast: with nothing published there is no
            # point scanning for rows that cannot exist.
            cursor.execute("""
                SELECT run_id, published_at
                FROM functions.los_publication
                WHERE singleton
            """)
            publication = cursor.fetchone()
            if publication is None:
                raise LosReadModelUnavailableError(
                    "LOS read model has not been published"
                )

            # The hotel dimension is a handful of rows and every fact row
            # carries a foreign key into it, so joining it per row - across a
            # hundred thousand of them - buys nothing that one lookup table
            # does not. Without the join the fact query is a covered range scan
            # over ix_reservation_los_daily_lookup and touches no heap at all.
            cursor.execute(
                "SELECT enterprise_id, hotel_name FROM functions.hotels"
            )
            hotel_names = {
                row["enterprise_id"]: row["hotel_name"]
                for row in cursor.fetchall()
            }

            # Deliberately unordered. Every consumer of this payload groups and
            # re-sorts it - the browser sorts its own output rows - so ordering
            # a hundred thousand rows here was a sort nobody read.
            cursor.execute("""
                SELECT arrival_date, enterprise_id, scenario, los,
                       booking_count, night_count
                FROM functions.reservation_los_daily
                WHERE run_id = %s
                  AND comparison_basis = %s
                  AND arrival_date BETWEEN %s AND %s
            """, (
                publication["run_id"], ly_comparison_basis, start_date, end_date
            ))
            rows = cursor.fetchall()

    if _is_stale(publication["published_at"]):
        logging.warning(
            "LOS publication is stale run_id=%s published_at=%s",
            publication["run_id"],
            publication["published_at"].isoformat(),
        )

    facts = []
    for row in rows:
        enterprise_id = row["enterprise_id"]
        # The fact table has a foreign key into the hotel dimension, so a miss
        # is impossible; naming the row by its enterprise ID rather than by null
        # keeps a broken invariant visible instead of silently regrouping every
        # unnamed hotel together, which is what the inner join used to risk.
        hotel_name = hotel_names.get(enterprise_id, enterprise_id)
        facts.append({
            "arrivalDate": row["arrival_date"].isoformat(),
            "hotelCode": hotel_name,
            "enterpriseId": enterprise_id,
            "hotelName": hotel_name,
            "scenario": row["scenario"],
            "los": int(row["los"]),
            "bookingCount": int(row["booking_count"]),
            "nightCount": int(row["night_count"]),
        })

    elapsed_ms = (monotonic() - started_at) * 1000
    logging.info(
        "LOS read model query completed run_id=%s row_count=%d duration_ms=%.1f",
        publication["run_id"],
        len(facts),
        elapsed_ms,
    )
    if elapsed_ms >= 500:
        logging.warning(
            "LOS read model query exceeded target duration_ms=%.1f", elapsed_ms
        )
    return LosFacts(facts, publication["run_id"], publication["published_at"])


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

    # The raw query reads live source data, so there is no publication to
    # validate a cached response against.
    return LosFacts([_fact_json(row) for row in rows], None, None)
