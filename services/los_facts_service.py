import logging
import os

from collections import namedtuple
from datetime import datetime, timedelta, timezone
from threading import Lock
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

# The identity on its own, resolvable without touching a fact row. The HTTP
# layer needs exactly this and nothing else to build the response validator, and
# it used to get it by running the whole query first - so an If-None-Match repeat
# paid the full range scan and the full Python row shaping before finding out it
# only had to answer 304.
LosPublication = namedtuple("LosPublication", ("run_id", "published_at"))

# Long enough to absorb the burst of requests one page load makes (Average LOS
# and LOS Distribution read the same publication, and the month picker fans out
# up to three ranges at once), short enough to stay well inside the 300 second
# freshness the route already advertises. Matches the sibling pointer in
# services/cost_publication_service.py.
_PUBLICATION_CACHE_SECONDS = float(
    os.environ.get("LOS_PUBLICATION_CACHE_SECONDS", "5")
)
_publication_cache = None
_publication_lock = Lock()


def _reset_publication_cache():
    """Test seam and explicit invalidation for a worker-local cache."""
    global _publication_cache
    with _publication_lock:
        _publication_cache = None


def fetch_los_publication():
    """Which publication the read model would answer from right now.

    One indexed single-row read against Database A, then reused for a few
    seconds. This is what lets the route put a validator on the response before
    deciding whether to build a body at all.
    """
    global _publication_cache
    with _publication_lock:
        cached = _publication_cache
        if cached is not None and monotonic() < cached[0]:
            return cached[1]

    ensure_los_schema()
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""
                SELECT run_id, published_at
                FROM functions.los_publication
                WHERE singleton
            """)
            row = cursor.fetchone()

    if row is None:
        raise LosReadModelUnavailableError(
            "LOS read model has not been published"
        )

    publication = LosPublication(row["run_id"], row["published_at"])
    with _publication_lock:
        _publication_cache = (
            monotonic() + _PUBLICATION_CACHE_SECONDS,
            publication,
        )
    return publication


def los_read_model_enabled():
    return os.environ.get("LOS_READ_MODEL_ENABLED", "false").lower() in {
        "1", "true", "yes", "on"
    }


# Two hotel identities, both load-bearing: hotelName is what the hotel-list
# route returns and what the filters send back, and enterpriseId is the stable
# key that survives a rename. The third field this used to carry, hotelCode, was
# a byte-for-byte copy of hotelName - on a year of facts, ~170k rows each
# repeating the same string twice.
def _fact_json(row):
    return {
        "arrivalDate": row["arrival_date"].isoformat(),
        "hotelName": row["hotel_name"],
        "enterpriseId": row["hotel_code"],
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


def _fetch_published_los_facts(
    start_date, end_date, ly_comparison_basis, publication=None
):
    ensure_los_schema()
    started_at = monotonic()
    # Resolving the publication first, on its own, is what lets the fact query
    # below take a constant run_id and read nothing but its own index. It also
    # fails fast: with nothing published there is no point scanning for rows that
    # cannot exist.
    #
    # The HTTP layer resolves it before this call now, because it needs the same
    # two fields to build the response validator. Taking it as an argument is
    # what stops that from becoming a second round trip for the same row.
    if publication is None:
        publication = fetch_los_publication()
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
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
                publication.run_id, ly_comparison_basis, start_date, end_date
            ))
            rows = cursor.fetchall()

    if _is_stale(publication.published_at):
        logging.warning(
            "LOS publication is stale run_id=%s published_at=%s",
            publication.run_id,
            publication.published_at.isoformat(),
        )

    facts = []
    for row in rows:
        enterprise_id = row["enterprise_id"]
        # The fact table has a foreign key into the hotel dimension, so a miss
        # is impossible; naming the row by its enterprise ID rather than by null
        # keeps a broken invariant visible instead of silently regrouping every
        # unnamed hotel together, which is what the inner join used to risk.
        facts.append({
            "arrivalDate": row["arrival_date"].isoformat(),
            "hotelName": hotel_names.get(enterprise_id, enterprise_id),
            "enterpriseId": enterprise_id,
            "scenario": row["scenario"],
            "los": int(row["los"]),
            "bookingCount": int(row["booking_count"]),
            "nightCount": int(row["night_count"]),
        })

    elapsed_ms = (monotonic() - started_at) * 1000
    logging.info(
        "LOS read model query completed run_id=%s row_count=%d duration_ms=%.1f",
        publication.run_id,
        len(facts),
        elapsed_ms,
    )
    if elapsed_ms >= 500:
        logging.warning(
            "LOS read model query exceeded target duration_ms=%.1f", elapsed_ms
        )
    return LosFacts(facts, publication.run_id, publication.published_at)


def fetch_los_facts(
    start_date, end_date, ly_comparison_basis, publication=None
):
    if los_read_model_enabled():
        return _fetch_published_los_facts(
            start_date, end_date, ly_comparison_basis, publication
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
