from threading import Lock
from time import monotonic

from database import pool
from queries.hotels import HOTELS_SQL


HOTEL_CACHE_TTL_SECONDS = 300

_cache = {}
_cache_lock = Lock()


def fetch_hotels(start_date, end_date, ly_comparison_basis):
    cache_key = (start_date, end_date, ly_comparison_basis)
    now = monotonic()

    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < HOTEL_CACHE_TTL_SECONDS:
            return list(cached[1])

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
            hotels = [row[0] for row in cursor.fetchall()]

    with _cache_lock:
        _cache[cache_key] = (monotonic(), tuple(hotels))
        if len(_cache) > 32:
            oldest_key = min(_cache, key=lambda key: _cache[key][0])
            del _cache[oldest_key]

    return hotels
