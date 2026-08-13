import gzip
import json
import logging
import os

from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from time import perf_counter
from zoneinfo import ZoneInfo

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
from services.supplement_schema_service import SupplementSchemaError
from services.supplement_service import (
    SupplementUnavailableError,
    fetch_supplement_detail,
    fetch_supplement_grid,
    fetch_supplement_status,
    list_supplement_hotels,
)
from services.supplement_sync_service import sync_supplement
from shared.pipeline import run_all_datasets, run_dataset


app = func.FunctionApp()

VALID_LY_COMPARISONS = {"sameDate", "sameWeekday"}
SUPPLEMENT_TIMER_HOUR = 2
SUPPLEMENT_TIMER_MINUTE = 15


def parse_date(value: str | None) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def json_response(payload, status_code=200, headers=None):
    return func.HttpResponse(
        json.dumps(payload, separators=(",", ":")),
        status_code=status_code,
        mimetype="application/json",
        headers=headers,
    )


def supplement_enabled():
    return os.environ.get("SUPPLEMENT_LIVE_ENABLED", "false").lower() in {
        "1", "true", "yes", "on"
    }


def supplement_timer_due(now_utc=None):
    """Map the Stockholm 02:15 schedule onto the two possible UTC hours.

    Linux Flex Consumption does not support WEBSITE_TIME_ZONE or TZ. The host
    invokes us at 00:15 and 01:15 UTC; this guard selects exactly one invocation
    using Stockholm DST rules. On the spring-forward day, nonexistent 02:15 is
    resolved to the first valid instant afterward (03:15 local).
    """
    current_utc = now_utc or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=timezone.utc)
    current_utc = current_utc.astimezone(timezone.utc)
    time_zone = ZoneInfo(
        os.environ.get("SUPPLEMENT_TIME_ZONE", "Europe/Stockholm")
    )
    local_date = current_utc.astimezone(time_zone).date()
    target_local = datetime.combine(
        local_date,
        time(SUPPLEMENT_TIMER_HOUR, SUPPLEMENT_TIMER_MINUTE),
        tzinfo=time_zone,
    )
    target_utc = target_local.astimezone(timezone.utc)
    return target_utc <= current_utc < target_utc + timedelta(minutes=45)


def supplement_disabled_response():
    return json_response({"error": "Supplement live data is not enabled"}, 503)


def list_parameter(req, name):
    return [item.strip() for item in (req.params.get(name) or "").split(",") if item.strip()]


def supplement_etag(payload, request_key):
    identity = "|".join([
        request_key,
        str(payload.get("runId", "none")),
        str(payload.get("status", "unknown")),
        str(payload.get("publishedAt", "none")),
    ])
    fingerprint = sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f'W/"supplement-{fingerprint}"'


def supplement_cached_response(req, payload, request_key):
    etag = supplement_etag(payload, request_key)
    common_headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=300",
        "Vary": "Accept-Encoding",
    }
    if req.headers.get("If-None-Match") == etag:
        return func.HttpResponse(status_code=304, headers=common_headers)

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if "gzip" in (req.headers.get("Accept-Encoding") or "").lower():
        body = gzip.compress(body, compresslevel=5)
        common_headers["Content-Encoding"] = "gzip"
    return func.HttpResponse(
        body=body,
        status_code=200,
        mimetype="application/json",
        headers=common_headers,
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


@app.route(
    route="supplement/status",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def supplement_status(req: func.HttpRequest) -> func.HttpResponse:
    if not supplement_enabled():
        return supplement_disabled_response()
    try:
        return supplement_cached_response(req, fetch_supplement_status(), "status")
    except SupplementSchemaError as error:
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception("Supplement status endpoint failed")
        return json_response({"error": "Unable to retrieve Supplement status"}, 500)


@app.route(
    route="supplement/hotels",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def supplement_hotels(req: func.HttpRequest) -> func.HttpResponse:
    if not supplement_enabled():
        return supplement_disabled_response()
    try:
        return supplement_cached_response(req, list_supplement_hotels(), "hotels")
    except SupplementSchemaError as error:
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception("Supplement hotels endpoint failed")
        return json_response({"error": "Unable to retrieve Supplement hotels"}, 500)


@app.route(
    route="supplement/grid",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def supplement_grid(req: func.HttpRequest) -> func.HttpResponse:
    if not supplement_enabled():
        return supplement_disabled_response()
    start_date = parse_date(req.params.get("startDate"))
    end_date = parse_date(req.params.get("endDate"))
    if start_date is None or end_date is None:
        return json_response(
            {"error": "startDate and endDate are required and must use YYYY-MM-DD"},
            400,
        )
    mode = req.params.get("mode") or "single"
    ly_basis = req.params.get("lyComparisonBasis") or "sameDate"
    inventory_basis = req.params.get("inventoryBasis") or "sellable"
    hotel_codes = sorted(set(list_parameter(req, "hotelCodes")))
    room_categories = sorted(set(list_parameter(req, "roomCategories")))
    started_at = perf_counter()
    try:
        payload = fetch_supplement_grid(
            start_date,
            end_date,
            mode=mode,
            hotel_codes=hotel_codes,
            room_categories=room_categories,
            ly_comparison_basis=ly_basis,
            inventory_basis=inventory_basis,
        )
        request_key = "|".join([
            start_date.isoformat(), end_date.isoformat(), mode, ly_basis,
            inventory_basis,
            ",".join(hotel_codes), ",".join(room_categories),
        ])
        response = supplement_cached_response(req, payload, request_key)
        logging.info(
            "Supplement grid served run_id=%s days=%s hotels=%s elapsed_ms=%.1f",
            payload.get("runId"), len(payload.get("dates", [])), len(hotel_codes),
            (perf_counter() - started_at) * 1000,
        )
        return response
    except ValueError as error:
        return json_response({"error": str(error)}, 400)
    except (SupplementUnavailableError, SupplementSchemaError) as error:
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception("Supplement grid endpoint failed")
        return json_response({"error": "Unable to retrieve Supplement grid"}, 500)


@app.route(
    route="supplement/detail",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def supplement_detail(req: func.HttpRequest) -> func.HttpResponse:
    if not supplement_enabled():
        return supplement_disabled_response()
    hotel_code = (req.params.get("hotelCode") or "").strip()
    stay_date = parse_date(req.params.get("stayDate"))
    category = (req.params.get("roomCategory") or "").strip() or None
    ly_basis = req.params.get("lyComparisonBasis") or "sameDate"
    inventory_basis = req.params.get("inventoryBasis") or "sellable"
    if not hotel_code or stay_date is None:
        return json_response(
            {"error": "hotelCode and a YYYY-MM-DD stayDate are required"},
            400,
        )
    try:
        started_at = perf_counter()
        payload = fetch_supplement_detail(
            hotel_code, stay_date, category, ly_basis, inventory_basis
        )
        request_key = "|".join([
            hotel_code, stay_date.isoformat(), category or "", ly_basis,
            inventory_basis,
        ])
        response = supplement_cached_response(req, payload, request_key)
        logging.info(
            "Supplement detail served run_id=%s elapsed_ms=%.1f",
            payload.get("runId"), (perf_counter() - started_at) * 1000,
        )
        return response
    except ValueError as error:
        return json_response({"error": str(error)}, 400)
    except (SupplementUnavailableError, SupplementSchemaError) as error:
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception("Supplement detail endpoint failed")
        return json_response({"error": "Unable to retrieve Supplement detail"}, 500)


@app.function_name(name="SupplementDataImport")
@app.route(
    route="supplement/import",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def supplement_import(req: func.HttpRequest) -> func.HttpResponse:
    if not supplement_enabled():
        return supplement_disabled_response()
    try:
        try:
            body = req.get_json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        mode = str(body.get("mode") or "delta").strip().lower()
        snapshot_from = parse_date(body.get("snapshotFrom"))
        snapshot_to = parse_date(body.get("snapshotTo"))
        return json_response(sync_supplement(mode, snapshot_from, snapshot_to))
    except ValueError as error:
        return json_response({"error": str(error)}, 400)
    except Exception:
        logging.exception("Manual Supplement import failed")
        return json_response({"error": "Unable to import Supplement data"}, 500)


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


@app.function_name(name="SupplementDataTimer")
@app.timer_trigger(
    # Flex Consumption timers use UTC. These are the CET and CEST candidates;
    # supplement_timer_due selects the Stockholm-local 02:15 occurrence.
    schedule="0 15 0,1 * * *",
    arg_name="mytimer",
    run_on_startup=False,
    use_monitor=True,
)
def supplement_data_timer(mytimer: func.TimerRequest) -> None:
    """Copy bounded Supplement snapshots from integration_db into PostgreSQL."""
    if not supplement_enabled():
        logging.info("SupplementDataTimer skipped because live data is disabled")
        return
    if not supplement_timer_due():
        logging.info("SupplementDataTimer skipped non-Stockholm UTC candidate")
        return
    if mytimer.past_due:
        logging.warning("SupplementDataTimer is running later than scheduled")
    result = sync_supplement("delta")
    logging.info("SupplementDataTimer finished result=%s", result)
