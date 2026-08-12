import logging

from datetime import date, datetime
from decimal import Decimal
from time import monotonic

from psycopg.rows import dict_row

from cost_database import cost_pool
from queries.cost_data import COST_DATA_QUERIES


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _json_row(row):
    return {
        "".join(
            word if index == 0 else word.capitalize()
            for index, word in enumerate(column.split("_"))
        ): _json_value(value)
        for column, value in row.items()
    }


def fetch_cost_data(start_date, end_date):
    parameters = {"start_date": start_date, "end_date": end_date}
    started_at = monotonic()
    datasets = {}

    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            for dataset, query in COST_DATA_QUERIES.items():
                cursor.execute(query, parameters)
                datasets[dataset] = [_json_row(row) for row in cursor.fetchall()]

    row_counts = {name: len(rows) for name, rows in datasets.items()}
    logging.info(
        "Cost data query completed start_date=%s end_date=%s "
        "row_counts=%s duration_ms=%.1f",
        start_date,
        end_date,
        row_counts,
        (monotonic() - started_at) * 1000,
    )

    return datasets, row_counts
