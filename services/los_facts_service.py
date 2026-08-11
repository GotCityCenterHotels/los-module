from psycopg.rows import dict_row

from database import pool
from queries.los_facts import LOS_FACTS_SQL


def fetch_los_facts(start_date, end_date, ly_comparison_basis):
    parameters = {
        "start_date": start_date,
        "end_date": end_date,
        "ly_comparison_basis": ly_comparison_basis,
    }

    with pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(LOS_FACTS_SQL, parameters)
            rows = cursor.fetchall()

    return [
        {
            "arrivalDate": row["arrival_date"].isoformat(),
            "hotelCode": row["hotel_code"],
            "scenario": row["scenario"],
            "los": int(row["los"]),
            "bookingCount": int(row["booking_count"]),
            "nightCount": int(row["night_count"]),
        }
        for row in rows
    ]
