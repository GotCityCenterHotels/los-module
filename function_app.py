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
from services.los_facts_service import (
    LosReadModelUnavailableError,
    fetch_los_facts,
    fetch_los_read_model_status,
)
from services.los_schema_service import LosSchemaError
from services.los_sync_service import sync_los
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
from services.import_job_schema_service import ImportJobSchemaError
from services.import_job_service import (
    claim_import_job,
    complete_import_job,
    create_import_job,
    fail_import_job,
    get_import_job,
    log_pool_stats,
)
from shared.pipeline import DATASETS, run_all_datasets, run_dataset


app = func.FunctionApp()

VALID_LY_COMPARISONS = {"sameDate", "sameWeekday"}
SUPPLEMENT_TIMER_HOUR = 2
SUPPLEMENT_TIMER_MINUTE = 15
IMPORT_QUEUE_NAME = "import-jobs"
IMPORT_MAX_DEQUEUE_COUNT = int(os.environ.get("IMPORT_MAX_DEQUEUE_COUNT", "3"))


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


def los_sync_enabled():
    return os.environ.get("LOS_SYNC_ENABLED", "false").lower() in {
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


def enqueue_import_job(message, job_type, operation, payload):
    job, created = create_import_job(job_type, operation, payload)
    # Re-emit an existing queued job as well. If a previous output binding
    # failed after the database insert, the next request repairs that orphan.
    # Duplicate queue deliveries are harmless because claiming is conditional.
    if created or job["status"] == "queued":
        message.set(json.dumps({"jobId": job["jobId"]}, separators=(",", ":")))
    status_url = f'/api/imports/{job["jobId"]}'
    return json_response(
        {
            "job": job,
            "statusUrl": status_url,
            "deduplicated": not created,
        },
        202,
        headers={"Location": status_url, "Retry-After": "2"},
    )


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
    except (LosReadModelUnavailableError, LosSchemaError) as error:
        return json_response({"error": str(error)}, 503)
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
    except (LosReadModelUnavailableError, LosSchemaError) as error:
        return json_response({"error": str(error)}, 503)
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
    route="los/status",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def los_status(req: func.HttpRequest) -> func.HttpResponse:
    try:
        return json_response(fetch_los_read_model_status())
    except LosSchemaError as error:
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception("LOS status endpoint failed")
        return json_response({"error": "Unable to retrieve LOS status"}, 500)


@app.function_name(name="LosDataImport")
@app.route(
    route="los/import",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
@app.queue_output(
    arg_name="message",
    queue_name=IMPORT_QUEUE_NAME,
    connection="AzureWebJobsStorage",
)
def los_import(req: func.HttpRequest, message: func.Out[str]) -> func.HttpResponse:
    if not los_sync_enabled():
        return json_response({"error": "LOS synchronization is not enabled"}, 503)
    try:
        try:
            body = req.get_json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        mode = str(body.get("mode") or "delta").strip().lower()
        if mode not in {"delta", "full"}:
            raise ValueError("mode must be delta or full")
        return enqueue_import_job(message, "los", mode, {"mode": mode})
    except ValueError as error:
        return json_response({"error": str(error)}, 400)
    except Exception:
        logging.exception("Manual LOS synchronization queueing failed")
        return json_response({"error": "Unable to queue LOS synchronization"}, 500)


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
    route="costdata/properties",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def cost_settings_hotels(req: func.HttpRequest) -> func.HttpResponse:
    try:
        return json_response(
            {"data": list_cost_settings_hotels()},
            headers={"Cache-Control": "no-store"},
        )
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
    auth_level=func.AuthLevel.ANONYMOUS,
)
@app.queue_output(
    arg_name="message",
    queue_name=IMPORT_QUEUE_NAME,
    connection="AzureWebJobsStorage",
)
def cost_data_import(
    req: func.HttpRequest,
    message: func.Out[str],
) -> func.HttpResponse:
    """Queue one cost dataset, or every dataset, for background import."""
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
        if dataset != "all" and dataset not in DATASETS:
            return json_response(
                {"error": f"Unknown dataset '{dataset}'. Allowed: {sorted(DATASETS)}"},
                400,
            )
        return enqueue_import_job(
            message,
            "cost",
            dataset,
            {"dataset": dataset},
        )
    except ValueError as error:
        return json_response({"error": str(error)}, 400)
    except RuntimeError as error:
        logging.exception("Cost data import queueing failed")
        return json_response({"error": str(error)}, 502)
    except Exception:
        logging.exception("Manual cost data import queueing failed")
        return json_response({"error": "Unable to queue cost data import"}, 500)


@app.function_name(name="ImportJobStatus")
@app.route(
    route="imports/{job_id}",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def import_job_status(req: func.HttpRequest) -> func.HttpResponse:
    try:
        job = get_import_job(req.route_params.get("job_id"))
        if job is None:
            return json_response({"error": "Import job was not found"}, 404)
        return json_response(
            {"job": job},
            headers={"Cache-Control": "no-store"},
        )
    except ValueError as error:
        return json_response({"error": str(error)}, 400)
    except ImportJobSchemaError as error:
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception("Import job status endpoint failed")
        return json_response({"error": "Unable to retrieve import job"}, 500)


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
@app.queue_output(
    arg_name="message",
    queue_name=IMPORT_QUEUE_NAME,
    connection="AzureWebJobsStorage",
)
def supplement_import(
    req: func.HttpRequest,
    message: func.Out[str],
) -> func.HttpResponse:
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
        if mode not in {"delta", "repair", "backfill"}:
            raise ValueError("mode must be delta, repair, or backfill")
        if mode != "delta":
            if snapshot_from is None or snapshot_to is None:
                raise ValueError("repair and backfill require snapshotFrom and snapshotTo")
            if snapshot_from > snapshot_to:
                raise ValueError("snapshotFrom cannot be after snapshotTo")
        return enqueue_import_job(
            message,
            "supplement",
            mode,
            {
                "mode": mode,
                "snapshotFrom": snapshot_from.isoformat() if snapshot_from else None,
                "snapshotTo": snapshot_to.isoformat() if snapshot_to else None,
            },
        )
    except ValueError as error:
        return json_response({"error": str(error)}, 400)
    except Exception:
        logging.exception("Manual Supplement import queueing failed")
        return json_response({"error": "Unable to queue Supplement import"}, 500)


@app.function_name(name="ImportJobWorker")
@app.queue_trigger(
    arg_name="message",
    queue_name=IMPORT_QUEUE_NAME,
    connection="AzureWebJobsStorage",
)
def import_job_worker(message: func.QueueMessage) -> None:
    envelope = message.get_json()
    job_id = envelope.get("jobId") if isinstance(envelope, dict) else None
    if not job_id:
        raise ValueError("Import queue message does not contain jobId")
    dequeue_count = max(int(getattr(message, "dequeue_count", 1) or 1), 1)
    job = claim_import_job(job_id, dequeue_count)
    if job is None:
        logging.info("Import queue delivery skipped job_id=%s", job_id)
        return

    started_at = perf_counter()
    try:
        payload = job["payload"] or {}
        if job["job_type"] == "cost":
            dataset = payload.get("dataset") or job["operation"]
            if dataset == "all":
                result = run_all_datasets()
                if result.get("status") != "success":
                    raise RuntimeError(f"Cost import completed with failures: {result}")
            else:
                dataset_result = run_dataset(dataset)
                result = {
                    "status": "success",
                    "results": [{"status": "success", **dataset_result}],
                }
        elif job["job_type"] == "supplement":
            mode = payload.get("mode") or job["operation"]
            result = sync_supplement(
                mode,
                parse_date(payload.get("snapshotFrom")),
                parse_date(payload.get("snapshotTo")),
            )
        elif job["job_type"] == "los":
            mode = payload.get("mode") or job["operation"]
            result = sync_los(mode)
        else:
            raise ValueError(f"Unsupported import job type: {job['job_type']}")

        result = {
            **result,
            "durationSeconds": round(perf_counter() - started_at, 3),
        }
        complete_import_job(job_id, result)
        logging.info(
            "Import job completed job_id=%s job_type=%s operation=%s "
            "attempt=%s elapsed_seconds=%.3f",
            job_id,
            job["job_type"],
            job["operation"],
            dequeue_count,
            perf_counter() - started_at,
        )
    except Exception as error:
        will_retry = dequeue_count < IMPORT_MAX_DEQUEUE_COUNT
        fail_import_job(job_id, error, will_retry)
        logging.exception(
            "Import job failed job_id=%s attempt=%s retry=%s",
            job_id,
            dequeue_count,
            will_retry,
        )
        raise
    finally:
        log_pool_stats(job_id)


@app.function_name(name="CostDataTimer")
@app.timer_trigger(
    schedule="0 5 0 * * *",
    arg_name="mytimer",
    run_on_startup=False,
    use_monitor=True,
)
@app.queue_output(
    arg_name="message",
    queue_name=IMPORT_QUEUE_NAME,
    connection="AzureWebJobsStorage",
)
def cost_data_timer(mytimer: func.TimerRequest, message: func.Out[str]) -> None:
    """Queue every cost dataset once per day at 00:05."""
    if mytimer.past_due:
        logging.warning("CostDataTimer is running later than scheduled")

    job, created = create_import_job("cost", "all", {"dataset": "all"})
    if created or job["status"] == "queued":
        message.set(json.dumps({"jobId": job["jobId"]}, separators=(",", ":")))
    logging.info(
        "CostDataTimer queued job_id=%s deduplicated=%s",
        job["jobId"],
        not created,
    )


@app.function_name(name="LosDataTimer")
@app.timer_trigger(
    schedule="0 20 0 * * *",
    arg_name="mytimer",
    run_on_startup=False,
    use_monitor=True,
)
@app.queue_output(
    arg_name="message",
    queue_name=IMPORT_QUEUE_NAME,
    connection="AzureWebJobsStorage",
)
def los_data_timer(mytimer: func.TimerRequest, message: func.Out[str]) -> None:
    """Queue daily LOS deltas and a Sunday full reconciliation."""
    if not los_sync_enabled():
        logging.info("LosDataTimer skipped because synchronization is disabled")
        return
    if mytimer.past_due:
        logging.warning("LosDataTimer is running later than scheduled")
    mode = "full" if datetime.now(timezone.utc).weekday() == 6 else "delta"
    job, created = create_import_job("los", mode, {"mode": mode})
    if created or job["status"] == "queued":
        message.set(json.dumps({"jobId": job["jobId"]}, separators=(",", ":")))
    logging.info(
        "LosDataTimer queued job_id=%s mode=%s deduplicated=%s",
        job["jobId"],
        mode,
        not created,
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
@app.queue_output(
    arg_name="message",
    queue_name=IMPORT_QUEUE_NAME,
    connection="AzureWebJobsStorage",
)
def supplement_data_timer(
    mytimer: func.TimerRequest,
    message: func.Out[str],
) -> None:
    """Queue a bounded Supplement delta from integration_db into PostgreSQL."""
    if not supplement_enabled():
        logging.info("SupplementDataTimer skipped because live data is disabled")
        return
    if not supplement_timer_due():
        logging.info("SupplementDataTimer skipped non-Stockholm UTC candidate")
        return
    if mytimer.past_due:
        logging.warning("SupplementDataTimer is running later than scheduled")
    job, created = create_import_job(
        "supplement",
        "delta",
        {"mode": "delta", "snapshotFrom": None, "snapshotTo": None},
    )
    if created or job["status"] == "queued":
        message.set(json.dumps({"jobId": job["jobId"]}, separators=(",", ":")))
    logging.info(
        "SupplementDataTimer queued job_id=%s deduplicated=%s",
        job["jobId"],
        not created,
    )
