import logging
import os

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from time import monotonic

from psycopg.rows import dict_row

from cost_database import cost_pool
from queries.cost_data import COST_DATA_QUERIES


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


def fetch_cost_data(start_date, end_date):
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
            dataset: _dataset_workers.submit(_fetch_dataset, query, parameters)
            for dataset, query in COST_DATA_QUERIES.items()
        }
        # Rebuilt in the declared order rather than in completion order, so the
        # response keys do not shuffle from one request to the next.
        datasets = {
            dataset: future.result() for dataset, future in pending.items()
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
