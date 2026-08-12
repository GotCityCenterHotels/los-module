import json
import logging

from datetime import date

import azure.functions as func

from services.hotels_service import fetch_hotels
from services.los_facts_service import fetch_los_facts
from services.cost_data_service import fetch_cost_data
from services.cost_settings_service import (
    fetch_cost_settings,
    list_cost_settings_hotels,
    save_cost_settings,
)
from services.cost_schema_service import CostSettingsSchemaError
from shared.pipeline import run_all_datasets, run_dataset


app = func.FunctionApp()

VALID_LY_COMPARISONS = {"sameDate", "sameWeekday"}


def parse_date(value: str | None) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def json_response(payload, status_code=200):
    return func.HttpResponse(
        json.dumps(payload, separators=(",", ":")),
        status_code=status_code,
        mimetype="application/json",
    )


def validate_facts_parameters(req):
    start_date = parse_date(req.params.get("startDate"))
    end_date = parse_date(req.params.get("endDate"))
    ly_comparison_basis = req.params.get("lyComparisonBasis") or "sameDate"

    if start_date is None:
        return None, json_response(
            {"error": "startDate is required and must use YYYY-MM-DD"},
            400,
        )

    if end_date is None:
        return None, json_response(
            {"error": "endDate is required and must use YYYY-MM-DD"},
            400,
        )

    if start_date > end_date:
        return None, json_response(
            {"error": "startDate cannot be after endDate"},
            400,
        )

    if ly_comparison_basis not in VALID_LY_COMPARISONS:
        return None, json_response(
            {
                "error": "Invalid lyComparisonBasis",
                "allowedValues": ["sameDate", "sameWeekday"],
            },
            400,
        )

    return (start_date, end_date, ly_comparison_basis), None


def validate_hotels_parameters(req):
    start_date_raw = req.params.get("startDate")
    end_date_raw = req.params.get("endDate")

    # Backward compatibility for cached clients that called the original
    # metadata route without parameters. Keep that fallback period-bounded.
    if not start_date_raw and not end_date_raw:
        current_year = date.today().year
        ly_comparison_basis = req.params.get("lyComparisonBasis") or "sameDate"
        if ly_comparison_basis not in VALID_LY_COMPARISONS:
            return None, json_response(
                {
                    "error": "Invalid lyComparisonBasis",
                    "allowedValues": ["sameDate", "sameWeekday"],
                },
                400,
            )
        return (
            date(current_year, 1, 1),
            date(current_year, 12, 31),
            ly_comparison_basis,
        ), None

    return validate_facts_parameters(req)


@app.route(
    route="los/hotels",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def los_hotels(req: func.HttpRequest) -> func.HttpResponse:
    parameters, error_response = validate_hotels_parameters(req)
    if error_response is not None:
        return error_response

    start_date, end_date, ly_comparison_basis = parameters

    try:
        hotels = fetch_hotels(start_date, end_date, ly_comparison_basis)

        return json_response(
            {
                "parameters": {
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "lyComparisonBasis": ly_comparison_basis,
                },
                "data": hotels,
            }
        )
    except Exception:
        logging.exception("LOS hotels endpoint failed")
        return json_response({"error": "Unable to retrieve hotels"}, 500)


@app.route(
    route="los/facts",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def los_facts(req: func.HttpRequest) -> func.HttpResponse:
    parameters, error_response = validate_facts_parameters(req)
    if error_response is not None:
        return error_response

    start_date, end_date, ly_comparison_basis = parameters

    try:
        rows = fetch_los_facts(start_date, end_date, ly_comparison_basis)
    except Exception:
        logging.exception("LOS facts endpoint failed")
        return json_response({"error": "Unable to retrieve LOS facts"}, 500)

    return json_response(
        {
            "parameters": {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "lyComparisonBasis": ly_comparison_basis,
            },
            "rowCount": len(rows),
            "data": rows,
        }
    )


@app.route(
    route="costdata/facts",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def cost_data_facts(req: func.HttpRequest) -> func.HttpResponse:
    start_date = parse_date(req.params.get("startDate"))
    end_date = parse_date(req.params.get("endDate"))

    if start_date is None:
        return json_response(
            {"error": "startDate is required and must use YYYY-MM-DD"},
            400,
        )
    if end_date is None:
        return json_response(
            {"error": "endDate is required and must use YYYY-MM-DD"},
            400,
        )
    if start_date > end_date:
        return json_response({"error": "startDate cannot be after endDate"}, 400)

    try:
        datasets, row_counts = fetch_cost_data(start_date, end_date)
    except Exception:
        logging.exception("Cost data endpoint failed")
        return json_response({"error": "Unable to retrieve cost data"}, 500)

    hotels = sorted(
        {
            row["hotelName"]
            for rows in datasets.values()
            for row in rows
            if row.get("hotelName")
        }
    )

    return json_response(
        {
            "parameters": {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
            },
            "rowCounts": row_counts,
            "hotels": hotels,
            "data": datasets,
        }
    )


@app.route(
    route="costdata/settings/hotels",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def cost_settings_hotels(req: func.HttpRequest) -> func.HttpResponse:
    try:
        return json_response({"data": list_cost_settings_hotels()})
    except CostSettingsSchemaError as error:
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception("Cost settings hotel endpoint failed")
        return json_response({"error": "Unable to retrieve properties"}, 500)


@app.route(
    route="costdata/settings/{enterprise_id}",
    methods=["GET", "PUT"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def cost_settings(req: func.HttpRequest) -> func.HttpResponse:
    enterprise_id = (req.route_params.get("enterprise_id") or "").strip()
    if not enterprise_id:
        return json_response({"error": "Enterprise ID is required"}, 400)

    try:
        if req.method == "GET":
            hotel_name = (req.params.get("hotelName") or "").strip() or None
            return json_response(
                {"data": fetch_cost_settings(enterprise_id, hotel_name)}
            )
        try:
            payload = req.get_json()
        except ValueError:
            return json_response({"error": "Request body must be valid JSON"}, 400)
        return json_response({"data": save_cost_settings(enterprise_id, payload)})
    except ValueError as error:
        return json_response({"error": str(error)}, 400)
    except CostSettingsSchemaError as error:
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception("Cost settings endpoint failed enterprise_id=%s", enterprise_id)
        action = "retrieve" if req.method == "GET" else "save"
        return json_response({"error": f"Unable to {action} cost settings"}, 500)


@app.function_name(name="CostDataImport")
@app.route(
    route="costdata/import",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def cost_data_import(req: func.HttpRequest) -> func.HttpResponse:
    """Manually transfer one cost dataset, or every dataset, to Database A."""
    try:
        try:
            body = req.get_json()
        except ValueError:
            body = {}

        if body is None:
            body = {}
        if not isinstance(body, dict):
            return json_response({"error": "Request body must be a JSON object"}, 400)

        dataset = body.get("dataset") or req.params.get("dataset") or "all"
        dataset = str(dataset).strip().lower()

        if dataset == "all":
            result = run_all_datasets()
        else:
            result = {
                "status": "success",
                "results": [run_dataset(dataset)],
            }

        status_code = 200 if result.get("status") == "success" else 207
        return json_response(result, status_code)
    except ValueError as error:
        return json_response({"error": str(error)}, 400)
    except Exception:
        logging.exception("Manual cost data import failed")
        return json_response({"error": "Unable to import cost data"}, 500)


@app.function_name(name="CostDataTimer")
@app.timer_trigger(
    schedule="0 5 0 * * *",
    arg_name="mytimer",
    run_on_startup=False,
    use_monitor=True,
)
def cost_data_timer(mytimer: func.TimerRequest) -> None:
    """Transfer every cost dataset once per day at 00:05."""
    if mytimer.past_due:
        logging.warning("CostDataTimer is running later than scheduled")

    logging.info("CostDataTimer started")
    result = run_all_datasets()
    logging.info("CostDataTimer finished result=%s", result)

    if result.get("status") != "success":
        raise RuntimeError(
            f"CostDataTimer finished with non-success status: {result}"
        )
