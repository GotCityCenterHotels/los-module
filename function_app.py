import json
import logging

from datetime import date, datetime
from decimal import Decimal

import azure.functions as func

from psycopg.rows import dict_row

from database import pool
from queries.los_average import LOS_AVERAGE_SQL
from queries.los_distribution import LOS_DISTRIBUTION_SQL

app = func.FunctionApp()


VALID_GRAINS = {
    "day",
    "month",
    "year",
}


VALID_LY_COMPARISONS = {
    "sameDate",
    "sameWeekday",
}


def parse_date(value: str | None) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(value)

    except ValueError:
        return None


def json_default(value):

    if isinstance(value, (date, datetime)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    raise TypeError(
        f"Type {type(value).__name__} "
        "is not JSON serializable"
    )

#s
@app.route(
    route="los/average",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def los_average(
    req: func.HttpRequest,
) -> func.HttpResponse:

    try:

        # =====================================================
        # 1. READ QUERY PARAMETERS
        # =====================================================

        start_date_raw = req.params.get(
            "startDate"
        )

        end_date_raw = req.params.get(
            "endDate"
        )

        grain = (
            req.params.get("grain")
            or "month"
        )

        hotel_name = req.params.get(
            "hotelName"
        )

        ly_comparison_basis = (
            req.params.get(
                "lyComparisonBasis"
            )
            or "sameDate"
        )


        # =====================================================
        # 2. NORMALIZE HOTEL
        # =====================================================

        if hotel_name:

            hotel_name = hotel_name.strip()

            if not hotel_name:
                hotel_name = None


        # =====================================================
        # 3. VALIDATE DATES
        # =====================================================

        start_date = parse_date(
            start_date_raw
        )

        end_date = parse_date(
            end_date_raw
        )


        if start_date is None:

            return func.HttpResponse(
                json.dumps({
                    "error":
                        "startDate is required "
                        "and must use YYYY-MM-DD"
                }),
                status_code=400,
                mimetype="application/json",
            )


        if end_date is None:

            return func.HttpResponse(
                json.dumps({
                    "error":
                        "endDate is required "
                        "and must use YYYY-MM-DD"
                }),
                status_code=400,
                mimetype="application/json",
            )


        if start_date > end_date:

            return func.HttpResponse(
                json.dumps({
                    "error":
                        "startDate cannot be "
                        "after endDate"
                }),
                status_code=400,
                mimetype="application/json",
            )


        # =====================================================
        # 4. VALIDATE GRAIN
        # =====================================================

        if grain not in VALID_GRAINS:

            return func.HttpResponse(
                json.dumps({
                    "error":
                        "Invalid grain",

                    "allowedValues": [
                        "day",
                        "month",
                        "year",
                    ]
                }),
                status_code=400,
                mimetype="application/json",
            )


        # =====================================================
        # 5. VALIDATE LY COMPARISON
        # =====================================================

        if (
            ly_comparison_basis
            not in VALID_LY_COMPARISONS
        ):

            return func.HttpResponse(
                json.dumps({
                    "error":
                        "Invalid "
                        "lyComparisonBasis",

                    "allowedValues": [
                        "sameDate",
                        "sameWeekday",
                    ]
                }),
                status_code=400,
                mimetype="application/json",
            )


        # =====================================================
        # 6. SQL PARAMETERS
        # =====================================================

        sql_parameters = (
            start_date,
            end_date,
            grain,
            hotel_name,
            ly_comparison_basis,
        )


        # =====================================================
        # 7. EXECUTE POSTGRESQL
        # =====================================================

        with pool.connection() as connection:

            with connection.cursor(
                row_factory=dict_row
            ) as cursor:

                cursor.execute(
                    LOS_AVERAGE_SQL,
                    sql_parameters,
                )

                rows = cursor.fetchall()


        # =====================================================
        # 8. CREATE RESPONSE
        # =====================================================

        response = {

            "parameters": {

                "startDate":
                    start_date.isoformat(),

                "endDate":
                    end_date.isoformat(),

                "grain":
                    grain,

                "hotelName":
                    hotel_name,

                "lyComparisonBasis":
                    ly_comparison_basis,
            },

            "rowCount":
                len(rows),

            "data":
                rows,
        }


        # =====================================================
        # 9. RETURN JSON
        # =====================================================

        return func.HttpResponse(
            json.dumps(
                response,
                default=json_default,
            ),
            status_code=200,
            mimetype="application/json",
        )


    except Exception:

        logging.exception(
            "LOS average endpoint failed"
        )

        return func.HttpResponse(
            json.dumps({
                "error":
                    "Unable to retrieve "
                    "LOS data"
            }),
            status_code=500,
            mimetype="application/json",
        )


@app.route(
    route="los/distribution",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def los_distribution(
    req: func.HttpRequest,
) -> func.HttpResponse:

    try:

        start_date_raw = req.params.get(
            "startDate"
        )

        end_date_raw = req.params.get(
            "endDate"
        )

        grain = (
            req.params.get("grain")
            or "month"
        )

        hotel_name = req.params.get(
            "hotelName"
        )

        ly_comparison_basis = (
            req.params.get(
                "lyComparisonBasis"
            )
            or "sameDate"
        )


        if hotel_name:

            hotel_name = (
                hotel_name.strip()
            )

            if not hotel_name:
                hotel_name = None


        start_date = parse_date(
            start_date_raw
        )

        end_date = parse_date(
            end_date_raw
        )


        if start_date is None:

            return func.HttpResponse(
                json.dumps({
                    "error":
                        "Invalid startDate. "
                        "Use YYYY-MM-DD."
                }),
                status_code=400,
                mimetype="application/json",
            )


        if end_date is None:

            return func.HttpResponse(
                json.dumps({
                    "error":
                        "Invalid endDate. "
                        "Use YYYY-MM-DD."
                }),
                status_code=400,
                mimetype="application/json",
            )


        if start_date > end_date:

            return func.HttpResponse(
                json.dumps({
                    "error":
                        "startDate cannot "
                        "be after endDate."
                }),
                status_code=400,
                mimetype="application/json",
            )


        if grain not in VALID_GRAINS:

            return func.HttpResponse(
                json.dumps({
                    "error":
                        "Invalid grain.",

                    "allowedValues": [
                        "day",
                        "month",
                        "year",
                    ]
                }),
                status_code=400,
                mimetype="application/json",
            )


        if (
            ly_comparison_basis
            not in VALID_LY_COMPARISONS
        ):

            return func.HttpResponse(
                json.dumps({
                    "error":
                        "Invalid lyComparisonBasis.",

                    "allowedValues": [
                        "sameDate",
                        "sameWeekday",
                    ]
                }),
                status_code=400,
                mimetype="application/json",
            )


        parameters = (
            start_date,
            end_date,
            grain,
            hotel_name,
            ly_comparison_basis,
        )


        with pool.connection() as connection:

            with connection.cursor(
                row_factory=dict_row
            ) as cursor:

                cursor.execute(
                    LOS_DISTRIBUTION_SQL,
                    parameters,
                )

                rows = cursor.fetchall()


        response = {

            "parameters": {
                "startDate":
                    start_date.isoformat(),

                "endDate":
                    end_date.isoformat(),

                "grain":
                    grain,

                "hotelName":
                    hotel_name,

                "lyComparisonBasis":
                    ly_comparison_basis,
            },

            "rowCount":
                len(rows),

            "data":
                rows,
        }


        return func.HttpResponse(
            json.dumps(
                response,
                default=json_default,
            ),
            status_code=200,
            mimetype="application/json",
        )


    except Exception:

        logging.exception(
            "LOS distribution request failed"
        )

        return func.HttpResponse(
            json.dumps({
                "error":
                    "Unable to retrieve "
                    "LOS distribution."
            }),
            status_code=500,
            mimetype="application/json",
        )