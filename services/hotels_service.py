import logging

from concurrent.futures import Future
from threading import Lock
from time import monotonic

from database import pool
from queries.hotels import HOTELS_SQL


HOTEL_CACHE_TTL_SECONDS = 300

_cache = {}
_inflight = {}
_cache_lock = Lock()


def fetch_hotels(start_date, end_date, ly_comparison_basis):
    cache_key = (start_date, end_date, ly_comparison_basis)
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

    if not owns_query:
        return list(pending.result())

    started_at = monotonic()
    try:
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
                hotels = [row[1] for row in cursor.fetchall()]

        hotel_tuple = tuple(hotels)
        with _cache_lock:
            _cache[cache_key] = (monotonic(), hotel_tuple)
            if len(_cache) > 32:
                oldest_key = min(_cache, key=lambda key: _cache[key][0])
                del _cache[oldest_key]

        pending.set_result(hotel_tuple)
        logging.info(
            "LOS hotels query completed start_date=%s end_date=%s "
            "ly_comparison_basis=%s row_count=%d duration_ms=%.1f",
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
