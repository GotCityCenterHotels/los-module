import logging

from time import monotonic

from psycopg.rows import dict_row

from database import pool
from queries.los_facts import LOS_FACTS_SQL


def fetch_los_facts(start_date, end_date, ly_comparison_basis):
    parameters = {
        "start_date": start_date,
        "end_date": end_date,
        "ly_comparison_basis": ly_comparison_basis,
    }

    started_at = monotonic()

    with pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
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

    return [
        {
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
        for row in rows
    ]
