import logging

from concurrent.futures import Future
from threading import Lock
from time import monotonic

from database import pool
from queries.hotels import HOTELS_SQL
from services.los_facts_service import (
    LosReadModelUnavailableError,
    los_read_model_enabled,
)
from services.los_schema_service import ensure_los_schema
from cost_database import cost_pool


HOTEL_CACHE_TTL_SECONDS = 300

_cache = {}
_inflight = {}
_cache_lock = Lock()


def _published_hotels(start_date, end_date, ly_comparison_basis):
    """The hotels present in the published aggregate for a period.

    Resolving the publication first turns the scan below into a covered range
    scan of ix_reservation_los_daily_lookup with a constant run_id, and keeps
    the hotel dimension out of a query that would otherwise join it across every
    matching fact row only to throw all but a handful of names away.
    """
    ensure_los_schema()
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT run_id FROM functions.los_publication WHERE singleton"
            )
            publication = cursor.fetchone()
            if publication is None:
                raise LosReadModelUnavailableError(
                    "LOS read model has not been published"
                )
            cursor.execute("""
                SELECT DISTINCT enterprise_id
                FROM functions.reservation_los_daily
                WHERE run_id = %s
                  AND comparison_basis = %s
                  AND arrival_date BETWEEN %s AND %s
            """, (publication[0], ly_comparison_basis, start_date, end_date))
            enterprise_ids = [row[0] for row in cursor.fetchall()]
            if not enterprise_ids:
                return []
            # Ordered in the database rather than in Python so the sequence the
            # browser renders keeps the collation it has always used, which is
            # not code-point order for Å, Ä, and Ö.
            cursor.execute("""
                SELECT DISTINCT hotel_name
                FROM functions.hotels
                WHERE enterprise_id = ANY(%s)
                ORDER BY hotel_name
            """, (enterprise_ids,))
            return [row[0] for row in cursor.fetchall()]


def _source_hotels(start_date, end_date, ly_comparison_basis):
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                HOTELS_SQL,
                {
                    "start_date": start_date,
                    "end_date": end_date,
                    "ly_comparison_basis": ly_comparison_basis,
                },
            )
            # Preserve the existing browser contract (display-name codes)
            # while the SQL resolves each name through the enterprise ID.
            return [row[1] for row in cursor.fetchall()]


def fetch_hotels(start_date, end_date, ly_comparison_basis):
    # Both read paths are cached now. The published one used to run uncached on
    # every call, which mattered little when nothing asked for it until a
    # dropdown was opened, and matters a lot now that the pages request it as
    # soon as they load. The TTL bounds how long a new publication can go
    # unnoticed here; the facts themselves carry a publication-derived validator
    # and are never served stale.
    read_model = los_read_model_enabled()
    cache_key = (read_model, start_date, end_date, ly_comparison_basis)
    now = monotonic()

    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < HOTEL_CACHE_TTL_SECONDS:
            return list(cached[1])

        pending = _inflight.get(cache_key)
        if pending is None:
            pending = Future()
            _inflight[cache_key] = pending
            owns_query = True
        else:
            owns_query = False

    # A caller that finds a query already running waits for that one rather than
    # starting a second identical one. Both pages request this on load, so
    # without the join two tabs opened together would double the work.
    if not owns_query:
        return list(pending.result())

    started_at = monotonic()
    try:
        hotels = (
            _published_hotels(start_date, end_date, ly_comparison_basis)
            if read_model
            else _source_hotels(start_date, end_date, ly_comparison_basis)
        )

        hotel_tuple = tuple(hotels)
        with _cache_lock:
            _cache[cache_key] = (monotonic(), hotel_tuple)
            if len(_cache) > 32:
                oldest_key = min(_cache, key=lambda key: _cache[key][0])
                del _cache[oldest_key]

        pending.set_result(hotel_tuple)
        logging.info(
            "LOS hotels query completed read_model=%s start_date=%s end_date=%s "
            "ly_comparison_basis=%s row_count=%d duration_ms=%.1f",
            read_model,
            start_date,
            end_date,
            ly_comparison_basis,
            len(hotels),
            (monotonic() - started_at) * 1000,
        )
        return hotels
    except BaseException as error:
        pending.set_exception(error)
        raise
    finally:
        with _cache_lock:
            if _inflight.get(cache_key) is pending:
                del _inflight[cache_key]
