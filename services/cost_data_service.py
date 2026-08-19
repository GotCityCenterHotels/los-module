import logging
import os

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from threading import Lock
from time import monotonic

from psycopg.rows import dict_row

from cost_database import cost_pool
from database import pool as source_pool
from queries.cost_data import COST_DATA_QUERIES
from queries.cost_spit import COST_SPIT_QUERIES
from services.cost_schema_service import ensure_cost_settings_schema


# The datasets are independent reads, so they are fetched together rather than
# one after another on a single connection.
#
# The cap is the whole design here. Each concurrent query holds a pooled
# connection, and this is not the only route using that pool - a request that
# took every connection would stall every other page for as long as it ran. One
# executor shared by all callers means the ceiling is global: however many cost
# requests arrive at once, this many connections is all they can ever hold
# between them. It is also held one below the pool size, so something else can
# always get in.
_CONFIGURED_CONCURRENCY = int(os.environ.get("COST_DATA_QUERY_CONCURRENCY", "3"))
COST_DATA_QUERY_CONCURRENCY = max(
    1, min(_CONFIGURED_CONCURRENCY, cost_pool.max_size - 1, len(COST_DATA_QUERIES))
)
_dataset_workers = ThreadPoolExecutor(
    max_workers=COST_DATA_QUERY_CONCURRENCY, thread_name_prefix="cost-data"
)

# SPIT reads Database B, not the imported final-state tables in Database A.
# Keeping its executor separate means the exact lifecycle reconstruction cannot
# consume the Database A connection ceiling used by the current/final statement.
_CONFIGURED_SPIT_CONCURRENCY = int(
    os.environ.get("COST_SPIT_QUERY_CONCURRENCY", "3")
)
COST_SPIT_QUERY_CONCURRENCY = max(
    1,
    min(
        _CONFIGURED_SPIT_CONCURRENCY,
        source_pool.max_size,
        len(COST_SPIT_QUERIES),
    ),
)
_spit_workers = ThreadPoolExecutor(
    max_workers=COST_SPIT_QUERY_CONCURRENCY,
    thread_name_prefix="cost-spit",
)
COST_SPIT_SUBMISSION_PRIORITY = (
    "distributionMix",
    "cleaningAllocations",
    "roomRevenue",
)

# Submission order, which is not the same thing as response order.
#
# The executor is three wide and there are seven datasets, so submissions queue.
# distributionRates is by far the longest - it aggregates reservation-level mix
# rows and prices them through the rulebook lateral - and it was declared last in
# COST_DATA_QUERIES, so it only started once six shorter queries had finished.
# Its whole duration was therefore appended to the request instead of overlapping
# anything. Starting it in the first wave lets the six short ones run underneath
# it.
#
# Anything not named here keeps its declared order behind those that are, so
# adding a dataset needs no change unless it turns out to be a slow one.
COST_DATA_SUBMISSION_PRIORITY = ("distributionRates", "cleaningAllocations")


def _submission_order(datasets):
    ranked = [name for name in COST_DATA_SUBMISSION_PRIORITY if name in datasets]
    return ranked + [name for name in datasets if name not in set(ranked)]
# Coordinators only: the actual SQL still runs through _dataset_workers and its
# global three-connection ceiling. Two threads let the selected and comparison
# ranges feed that queue together from one HTTP invocation.
_range_workers = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="cost-data-range",
)

# This is the server-side half of the 60-second browser cache advertised by the
# route. It does not widen the staleness window: it lets users and tabs inside
# the window share the same seven-query build instead of each rebuilding it.
# Four entries cover this year and last year for the two comparison bases while
# keeping a worker from retaining every range it has ever served.
COST_DATA_CACHE_TTL_SECONDS = float(
    os.environ.get(
        "COST_DATA_RESULT_CACHE_SECONDS",
        os.environ.get("COST_DATA_MAX_AGE_SECONDS", "60"),
    )
)
COST_DATA_CACHE_MAX_ENTRIES = max(
    1,
    int(os.environ.get("COST_DATA_RESULT_CACHE_MAX_ENTRIES", "4")),
)
_result_cache = {}
_result_inflight = {}
_result_cache_lock = Lock()


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _camel_case(column):
    return "".join(
        word if index == 0 else word.capitalize()
        for index, word in enumerate(column.split("_"))
    )


def _json_rows(rows):
    """Shape a whole result set for JSON.

    Every row in a result set carries the same columns in the same order, so the
    snake_case to camelCase rewrite - a split, a capitalize per word, and a join
    - is done once for the set instead of once per cell. On six datasets of a
    few thousand rows each that was tens of thousands of identical string
    rebuilds per request.
    """
    if not rows:
        return []
    names = [_camel_case(column) for column in rows[0]]
    return [
        dict(zip(names, [_json_value(value) for value in row.values()]))
        for row in rows
    ]


def _fetch_dataset(query, parameters):
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, parameters)
            return _json_rows(cursor.fetchall())


def _fetch_spit_dataset(query, parameters):
    with source_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, parameters)
            return _json_rows(cursor.fetchall())


def _fetch_cost_data_uncached(start_date, end_date):
    # Two of these datasets read tables that migration 016 creates, and this runs
    # BEFORE fetch_all_cost_settings() in the facts route - which was the only
    # thing applying migrations on this path. On a Database A that had not yet
    # taken 016, the first Cost Data request therefore failed with UndefinedTable
    # and the route answered "Unable to retrieve cost data" for the whole page,
    # taking down the five datasets that were fine along with the two that were
    # not. Every other cost read already opens this way; this one was missed.
    #
    # Not a real cost: the check short-circuits on a flag after the first call in
    # a worker, and is one round trip before that.
    ensure_cost_settings_schema()

    parameters = {"start_date": start_date, "end_date": end_date}
    started_at = monotonic()

    if COST_DATA_QUERY_CONCURRENCY == 1:
        # A pool too small to spare a connection: one connection, one query at a
        # time, exactly as before.
        with cost_pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                datasets = {}
                for dataset, query in COST_DATA_QUERIES.items():
                    cursor.execute(query, parameters)
                    datasets[dataset] = _json_rows(cursor.fetchall())
    else:
        pending = {
            dataset: _dataset_workers.submit(
                _fetch_dataset, COST_DATA_QUERIES[dataset], parameters
            )
            for dataset in _submission_order(COST_DATA_QUERIES)
        }
        # Rebuilt in the declared order rather than in submission or completion
        # order, so the response keys do not shuffle from one request to the next.
        datasets = {
            dataset: pending[dataset].result() for dataset in COST_DATA_QUERIES
        }

    row_counts = {name: len(rows) for name, rows in datasets.items()}
    logging.info(
        "Cost data query completed start_date=%s end_date=%s "
        "row_counts=%s concurrency=%d duration_ms=%.1f",
        start_date,
        end_date,
        row_counts,
        COST_DATA_QUERY_CONCURRENCY,
        (monotonic() - started_at) * 1000,
    )

    return datasets, row_counts


def _cached_result(key, build):
    """Share a fresh or in-progress immutable result for one cache key."""
    now = monotonic()
    with _result_cache_lock:
        cached = _result_cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

        pending = _result_inflight.get(key)
        if pending is None:
            pending = Future()
            _result_inflight[key] = pending
            owns_query = True
        else:
            owns_query = False

    if not owns_query:
        return pending.result()

    try:
        result = build()
    except BaseException as error:
        pending.set_exception(error)
        raise
    else:
        with _result_cache_lock:
            _result_cache[key] = (
                monotonic() + COST_DATA_CACHE_TTL_SECONDS,
                result,
            )
            if len(_result_cache) > COST_DATA_CACHE_MAX_ENTRIES:
                oldest = min(
                    _result_cache,
                    key=lambda entry: _result_cache[entry][0],
                )
                del _result_cache[oldest]
        pending.set_result(result)
        return result
    finally:
        with _result_cache_lock:
            if _result_inflight.get(key) is pending:
                del _result_inflight[key]


def fetch_cost_data(start_date, end_date, publication_version=None):
    """Fetch a date range, sharing fresh and in-progress identical results.

    The returned mappings are treated as read-only by the HTTP layer. A cache
    hit therefore needs no copy of what can be tens of thousands of rows.
    """
    key = ("single", publication_version, start_date, end_date)
    return _cached_result(
        key,
        lambda: _fetch_cost_data_uncached(start_date, end_date),
    )


def fetch_cost_data_ranges(ranges, publication_version=None):
    """Fetch named ranges together inside one API invocation.

    ``ranges`` is an iterable of ``(label, start_date, end_date)`` triples.
    Labels must be unique because they become response keys.
    """
    ranges = tuple(ranges)
    labels = [label for label, _start, _end in ranges]
    if not ranges:
        raise ValueError("At least one Cost Data range is required")
    if len(labels) != len(set(labels)):
        raise ValueError("Cost Data range labels must be unique")

    def build():
        pending = {
            label: _range_workers.submit(
                fetch_cost_data,
                start_date,
                end_date,
                publication_version,
            )
            for label, start_date, end_date in ranges
        }
        return {label: future.result() for label, future in pending.items()}

    key = ("ranges", publication_version, ranges)
    return _cached_result(
        key,
        build,
    )


def fetch_cost_spit_data(
    start_date,
    end_date,
    cutoff_date,
    publication_version=None,
):
    """Rebuild Cost Data exactly as Database B stood at ``cutoff_date``.

    Unlike the imported tables, the source retains created and cancelled dates.
    Each query applies the lifecycle boundary before aggregation, so a booking
    created later is absent and a booking cancelled later is still present.  An
    empty result is a valid SPIT answer, not an availability failure.
    """

    def build():
        parameters = {
            "start_date": start_date,
            "end_date": end_date,
            "cutoff_date": cutoff_date,
        }
        order = [
            *(
                dataset
                for dataset in COST_SPIT_SUBMISSION_PRIORITY
                if dataset in COST_SPIT_QUERIES
            ),
            *(
                dataset
                for dataset in COST_SPIT_QUERIES
                if dataset not in COST_SPIT_SUBMISSION_PRIORITY
            ),
        ]
        pending = {
            dataset: _spit_workers.submit(
                _fetch_spit_dataset,
                COST_SPIT_QUERIES[dataset],
                parameters,
            )
            for dataset in order
        }
        datasets = {
            dataset: pending[dataset].result()
            for dataset in COST_SPIT_QUERIES
        }
        counts = {dataset: len(rows) for dataset, rows in datasets.items()}
        logging.info(
            "Cost SPIT lifecycle read completed start_date=%s end_date=%s "
            "cutoff_date=%s rows=%s",
            start_date,
            end_date,
            cutoff_date,
            sum(counts.values()),
        )
        return datasets, counts

    return _cached_result(
        (
            "spit",
            publication_version,
            start_date,
            end_date,
            cutoff_date,
        ),
        build,
    )
