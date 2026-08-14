"""Compare published Database A LOS rows with the read-only source query."""

import argparse
import json

from collections import Counter
from psycopg.rows import dict_row

from queries.los_facts import LOS_FACTS_SQL
from shared.db import get_export_connection, get_import_connection


PUBLISHED_SQL = """
SELECT daily.arrival_date, daily.enterprise_id AS hotel_code,
       hotel.hotel_name, daily.scenario, daily.los,
       daily.booking_count, daily.night_count
FROM functions.los_publication publication
JOIN functions.reservation_los_daily daily
  ON daily.run_id = publication.run_id
JOIN functions.hotels hotel ON hotel.enterprise_id = daily.enterprise_id
WHERE publication.singleton
  AND daily.comparison_basis = %(ly_comparison_basis)s
  AND daily.arrival_date BETWEEN %(start_date)s AND %(end_date)s
"""


def _key(row):
    return (
        row["arrival_date"],
        row["hotel_code"],
        row["hotel_name"],
        row["scenario"],
        int(row["los"]),
        int(row["booking_count"]),
        int(row["night_count"]),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--basis", choices=("sameDate", "sameWeekday"), required=True
    )
    args = parser.parse_args()
    parameters = {
        "start_date": args.start,
        "end_date": args.end,
        "ly_comparison_basis": args.basis,
    }

    with get_export_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SET LOCAL work_mem = '64MB'")
            cursor.execute(LOS_FACTS_SQL, parameters)
            source = Counter(_key(row) for row in cursor.fetchall())
    with get_import_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(PUBLISHED_SQL, parameters)
            published = Counter(_key(row) for row in cursor.fetchall())

    source_only = source - published
    published_only = published - source
    result = {
        "sourceRows": sum(source.values()),
        "publishedRows": sum(published.values()),
        "sourceOnlyRows": sum(source_only.values()),
        "publishedOnlyRows": sum(published_only.values()),
        "identical": not source_only and not published_only,
    }
    print(json.dumps(result, default=str, separators=(",", ":")))
    if not result["identical"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
