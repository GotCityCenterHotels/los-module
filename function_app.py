import gzip
import json
import logging
import os

from collections import OrderedDict
from concurrent.futures import Future
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from threading import Lock
from time import perf_counter
from zoneinfo import ZoneInfo

import azure.functions as func

from services.hotels_service import fetch_hotels
from services.los_facts_service import (
    LOS_GRAINS,
    LosReadModelUnavailableError,
    fetch_los_facts,
    fetch_los_publication,
    fetch_los_read_model_status,
    los_read_model_enabled,
)
from services.los_schema_service import LosSchemaError
from services.cost_data_service import (
    fetch_cost_data,
    fetch_cost_data_ranges,
    fetch_cost_spit_adjustments,
)
from services.cost_publication_service import fetch_cost_publication_version
from services.cost_settings_service import (
    fetch_all_cost_settings,
    fetch_cost_settings,
    list_cost_settings_hotels,
    save_cost_settings,
)
from services.cost_schema_service import CostSettingsSchemaError
from services.cost_source_service import (
    CostSourceUnavailableError,
    fetch_cost_sources,
    list_matching_rates,
    list_travel_agencies,
)
from services.supplement_schema_service import SupplementSchemaError
from services.supplement_service import (
    SupplementUnavailableError,
    fetch_supplement_detail,
    fetch_supplement_grid,
    fetch_supplement_status,
    list_supplement_hotels,
)
from services.import_job_schema_service import ImportJobSchemaError
from services.import_job_service import (
    claim_import_job,
    complete_import_job,
    create_import_job,
    fail_import_job,
    get_import_job,
    log_pool_stats,
)


app = func.FunctionApp()

VALID_LY_COMPARISONS = {"sameDate", "sameWeekday"}
SUPPLEMENT_TIMER_HOUR = 2
SUPPLEMENT_TIMER_MINUTE = 15
IMPORT_QUEUE_NAME = "import-jobs"
IMPORT_MAX_DEQUEUE_COUNT = int(os.environ.get("IMPORT_MAX_DEQUEUE_COUNT", "3"))

# Static Web Apps aborts a linked-backend call at ~45s, so an unbounded range is
# not "slow", it is a guaranteed "Backend call failure". 400 days covers a full
# year plus a comparison window. services/supplement_service.py already enforces
# the equivalent MAX_GRID_DAYS; this applies the same guard to LOS and cost.
MAX_RANGE_DAYS = int(os.environ.get("MAX_QUERY_RANGE_DAYS", "400"))

# Short on purpose. Cost facts change when an import runs, and the operator
# reloading the page may be the person who just triggered one, so this buys the
# repeats within a single sitting without holding a stale answer past the point
# anyone would notice. The sibling cost routes already sit at 120-300.
COST_DATA_MAX_AGE_SECONDS = int(os.environ.get("COST_DATA_MAX_AGE_SECONDS", "60"))
COST_DATA_RESPONSE_CACHE_MAX_ENTRIES = max(
    1,
    int(os.environ.get("COST_DATA_RESPONSE_CACHE_MAX_ENTRIES", "8")),
)
# Part of the validator because a deployment can change response semantics
# without importing facts or saving settings. Increment this when the Cost Data
# response contract or calculation changes.
COST_DATA_RESPONSE_SCHEMA_VERSION = 3

# LOS facts is the largest response the app produces - a year of per-hotel,
# per-LOS rows - and it had no server-side byte cache at all, only a validator.
# Two browsers, two tabs, or the Average LOS and LOS Distribution pages asking
# for the same published range each paid the whole query, the whole Python row
# shaping, and the whole gzip. Four entries cover a year and its neighbours for
# both comparison bases.
LOS_FACTS_MAX_AGE_SECONDS = int(os.environ.get("LOS_FACTS_MAX_AGE_SECONDS", "300"))
LOS_FACTS_RESPONSE_CACHE_MAX_ENTRIES = max(
    1,
    int(os.environ.get("LOS_FACTS_RESPONSE_CACHE_MAX_ENTRIES", "4")),
)
# Increment when the LOS facts response contract or row shape changes, so a
# deployment cannot serve new-shaped bytes under an old validator.
LOS_FACTS_RESPONSE_SCHEMA_VERSION = 2
# Matches the server-side TTL in services/hotels_service.py and the browser-side
# one in frontend/los-api.js, so all three agree on how long a hotel list stands.
HOTEL_LIST_MAX_AGE_SECONDS = int(
    os.environ.get("HOTEL_LIST_MAX_AGE_SECONDS", "300")
)


# The import pipeline and the two sync services are reachable only from the queue
# worker and the enqueue routes, never from a read path - but Azure Functions
# imports this module to index its triggers, so every HTTP cold start was paying
# for their module graph anyway: about 37ms measured, and more on a 1-vCPU
# instance. Importing them on use defers that to the worker process that needs
# them.
#
# Wrappers rather than bare inline imports because the names have to stay on this
# module: tests patch function_app.run_dataset, and a route or worker that had
# imported the real function directly could not be intercepted. It is the pattern
# shared/pipeline.py already uses for cost_mix_export_service.


def run_dataset(dataset):
    from shared.pipeline import run_dataset as pipeline_run_dataset

    return pipeline_run_dataset(dataset)


def run_all_datasets():
    from shared.pipeline import run_all_datasets as pipeline_run_all

    return pipeline_run_all()


def dataset_names():
    """The importable dataset names, for request validation."""
    from shared.pipeline import DATASETS

    return DATASETS


def sync_los(mode):
    from services.los_sync_service import sync_los as run_los_sync

    return run_los_sync(mode)


def sync_supplement(mode, snapshot_from, snapshot_to):
    from services.supplement_sync_service import (
        sync_supplement as run_supplement_sync,
    )

    return run_supplement_sync(mode, snapshot_from, snapshot_to)


def validate_range_span(start_date: date, end_date: date):
    span_days = (end_date - start_date).days + 1
    if span_days > MAX_RANGE_DAYS:
        return json_response(
            {
                "error": (
                    f"Requested range covers {span_days} days, which exceeds the "
                    f"{MAX_RANGE_DAYS} day maximum. Narrow the date range."
                ),
                "maxRangeDays": MAX_RANGE_DAYS,
            },
            400,
        )
    return None


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


def compressed_json_response(req, payload, status_code=200, headers=None):
    """JSON, gzipped when the client accepts it.

    The fact payloads are the largest responses in the app - a year of per-hotel,
    per-LOS rows, or six cost datasets in one body - and this shape of JSON
    compresses roughly 10:1. Shrinking the transfer is the cheapest way to keep
    these routes inside the ~45s Static Web Apps proxy budget.
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response_headers = dict(headers or {})
    response_headers["Vary"] = "Accept-Encoding"
    # Below roughly one packet, gzip costs more than it saves.
    if len(body) > 1400 and "gzip" in (req.headers.get("Accept-Encoding") or "").lower():
        body = gzip.compress(body, compresslevel=5)
        response_headers["Content-Encoding"] = "gzip"
    return func.HttpResponse(
        body=body,
        status_code=status_code,
        mimetype="application/json",
        headers=response_headers,
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
    return content_etag("supplement", identity)


def content_etag(prefix, identity):
    fingerprint = sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f'W/"{prefix}-{fingerprint}"'


def cached_json_response(req, payload, etag):
    """A revalidatable JSON response.

    Everything these read routes serve is a published snapshot: identical
    parameters against an unchanged publication are identical bytes. Saying so
    lets the browser answer a repeat itself - the same range opened again, or
    the sibling page that shows the same facts differently - instead of paying
    for the query and the transfer a second time.
    """
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


def supplement_cached_response(req, payload, request_key):
    return cached_json_response(req, payload, supplement_etag(payload, request_key))


def content_hash_response(req, payload, prefix, max_age):
    """A revalidatable JSON response for a route with no publication to name.

    The Supplement and LOS reads validate against the publication that produced
    them, which lets them answer a repeat without rebuilding anything. Cost data
    has no such marker: the seven fact tables carry a last_updated_at but no
    index on it, so a watermark query would sequentially scan all seven - more
    work than the request it was meant to save - and the settings tables it also
    depends on carry no timestamp at all.

    So the validator is the body itself. It cannot skip building the response,
    but it is exactly correct - identical bytes mean an identical ETag - and it
    turns a repeat into a 304 with nothing on the wire. max-age is what actually
    removes the request, and is deliberately short because this data changes
    when an import runs, which an operator may have just triggered themselves.
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    etag = content_etag(prefix, sha256(body).hexdigest())
    headers = {
        "ETag": etag,
        "Cache-Control": f"private, max-age={max_age}",
        "Vary": "Accept-Encoding",
    }
    if req.headers.get("If-None-Match") == etag:
        return func.HttpResponse(status_code=304, headers=headers)
    if len(body) > 1400 and "gzip" in (req.headers.get("Accept-Encoding") or "").lower():
        body = gzip.compress(body, compresslevel=5)
        headers["Content-Encoding"] = "gzip"
    return func.HttpResponse(
        body=body,
        status_code=200,
        mimetype="application/json",
        headers=headers,
    )


class VersionedResponseCache:
    """Complete response bytes, built once per publication and reused.

    Every read route here serves a published snapshot, so identical parameters
    against an unchanged publication are identical bytes. Two things follow, and
    this holds both: a matching validator can be answered before any query runs,
    and a miss can be built once for however many callers want it at the same
    moment rather than once each.

    Both encodings are kept. Which one goes on the wire is the client's choice,
    and compressing the same body again per request was the second-largest CPU
    cost on these routes after the query itself.
    """

    def __init__(self, name, max_age_seconds, max_entries):
        self.name = name
        self.max_age_seconds = max_age_seconds
        self.max_entries = max_entries
        # Exposed rather than private: the tests reach for these to isolate one
        # case from the next, and a worker-local cache has no other seam.
        self.entries = OrderedDict()
        self.inflight = {}
        self._lock = Lock()

    def _headers(self, etag):
        return {
            "ETag": etag,
            "Cache-Control": f"private, max-age={self.max_age_seconds}",
            "Vary": "Accept-Encoding",
        }

    def _respond(self, req, entry, etag):
        headers = self._headers(etag)
        accepts_gzip = "gzip" in (
            req.headers.get("Accept-Encoding") or ""
        ).lower()
        if accepts_gzip:
            headers["Content-Encoding"] = "gzip"
        return func.HttpResponse(
            body=entry[1] if accepts_gzip else entry[0],
            status_code=200,
            mimetype="application/json",
            headers=headers,
        )

    def not_modified(self, etag):
        return func.HttpResponse(status_code=304, headers=self._headers(etag))

    def respond(self, req, cache_key, etag, payload_factory):
        """Answer from the validator, then the cache, then a shared build.

        ``payload_factory`` returns ``(payload, cacheable)``. A degraded payload
        - one assembled after a dependency failed - is served but not retained.
        """
        if req.headers.get("If-None-Match") == etag:
            return self.not_modified(etag)

        with self._lock:
            entry = self.entries.pop(cache_key, None)
            if entry is not None:
                self.entries[cache_key] = entry
                return self._respond(req, entry, etag)

            pending = self.inflight.get(cache_key)
            if pending is None:
                pending = Future()
                self.inflight[cache_key] = pending
                owns_build = True
            else:
                owns_build = False

        if not owns_build:
            return self._respond(req, pending.result(), etag)

        try:
            payload, cacheable = payload_factory()
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            entry = (body, gzip.compress(body, compresslevel=5))
        except BaseException as error:
            pending.set_exception(error)
            raise
        else:
            if cacheable:
                with self._lock:
                    self.entries[cache_key] = entry
                    while len(self.entries) > self.max_entries:
                        self.entries.popitem(last=False)
            pending.set_result(entry)
            return self._respond(req, entry, etag)
        finally:
            with self._lock:
                if self.inflight.get(cache_key) is pending:
                    del self.inflight[cache_key]


_cost_response_bytes = VersionedResponseCache(
    "costdata",
    COST_DATA_MAX_AGE_SECONDS,
    COST_DATA_RESPONSE_CACHE_MAX_ENTRIES,
)
_los_facts_response_bytes = VersionedResponseCache(
    "los-facts",
    LOS_FACTS_MAX_AGE_SECONDS,
    LOS_FACTS_RESPONSE_CACHE_MAX_ENTRIES,
)

# The names the tests and the rest of this module already use. Same objects, so
# clearing either one clears the cache it names.
_cost_response_cache = _cost_response_bytes.entries
_cost_response_inflight = _cost_response_bytes.inflight


def versioned_cost_response(req, cache_key, etag, payload_factory):
    return _cost_response_bytes.respond(req, cache_key, etag, payload_factory)


def parse_facts_grain(req):
    """The date grain to roll the LOS response up to.

    Defaults to day, which is the storage grain and the previous behaviour, so a
    cached client that has not learned to send the parameter still gets exactly
    the response it did before.
    """
    grain = req.params.get("grain") or "day"
    if grain not in LOS_GRAINS:
        return None, json_response(
            {
                "error": "Invalid grain",
                "allowedValues": list(LOS_GRAINS),
            },
            400,
        )
    return grain, None


def _los_facts_payload(
    start_date, end_date, ly_comparison_basis, grain, facts
):
    return {
        "parameters": {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "lyComparisonBasis": ly_comparison_basis,
            "grain": grain,
        },
        "runId": facts.run_id,
        "rowCount": len(facts.rows),
        "data": facts.rows,
    }


def shift_cost_comparison_date(value, basis):
    if basis == "sameWeekday":
        return value - timedelta(days=364)
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        # 29 February compares with the last valid day of February, matching
        # LosFormat.lastYearDate in the browser.
        return value.replace(year=value.year - 1, day=28)


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

    span_error = validate_range_span(start_date, end_date)
    if span_error is not None:
        return None, span_error

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

        # Both pages ask for this the moment they load, and the answer is already
        # held for five minutes on the server and another five in the browser's
        # own store - but without a validator or a max-age the request itself was
        # still made every time, and answered with the full body. There is no
        # publication to name here (the raw-source path has none, and the read
        # model path does not surface its run_id through fetch_hotels), so the
        # body is its own validator: a repeat inside the window costs nothing,
        # and a repeat after it costs a 304.
        return content_hash_response(
            req,
            {
                "parameters": {
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "lyComparisonBasis": ly_comparison_basis,
                },
                "data": hotels,
            },
            "los-hotels",
            HOTEL_LIST_MAX_AGE_SECONDS,
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

    grain, grain_error = parse_facts_grain(req)
    if grain_error is not None:
        return grain_error

    # The raw-query fallback reads live source data, so it has no publication to
    # validate against and must not be cached or reused.
    if not los_read_model_enabled():
        try:
            facts = fetch_los_facts(
                start_date, end_date, ly_comparison_basis, None, grain
            )
        except (LosReadModelUnavailableError, LosSchemaError) as error:
            return json_response({"error": str(error)}, 503)
        except Exception:
            logging.exception("LOS facts endpoint failed")
            return json_response({"error": "Unable to retrieve LOS facts"}, 500)
        return compressed_json_response(req, _los_facts_payload(
            start_date, end_date, ly_comparison_basis, grain, facts
        ))

    # Resolving the publication first is the whole point. It is one indexed
    # single-row read, cached for a few seconds, and it is everything the
    # validator needs - so an If-None-Match repeat is answered here, before the
    # range scan and before a hundred thousand rows are shaped into JSON. That
    # repeat used to cost exactly as much as a full 200.
    try:
        publication = fetch_los_publication()
    except (LosReadModelUnavailableError, LosSchemaError) as error:
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception("LOS publication lookup failed")
        return json_response({"error": "Unable to retrieve LOS facts"}, 500)

    # Average LOS and LOS Distribution show the same published facts, so opening
    # one after the other - or reopening either - is a repeat of a request whose
    # answer cannot have changed while the publication has not.
    etag = content_etag("los-facts", "|".join([
        str(LOS_FACTS_RESPONSE_SCHEMA_VERSION),
        start_date.isoformat(),
        end_date.isoformat(),
        ly_comparison_basis,
        grain,
        str(publication.run_id),
        publication.published_at.isoformat(),
    ]))
    cache_key = (
        LOS_FACTS_RESPONSE_SCHEMA_VERSION,
        publication.run_id,
        start_date,
        end_date,
        ly_comparison_basis,
        grain,
    )

    def build_payload():
        facts = fetch_los_facts(
            start_date, end_date, ly_comparison_basis, publication, grain
        )
        return _los_facts_payload(
            start_date, end_date, ly_comparison_basis, grain, facts
        ), True

    try:
        return _los_facts_response_bytes.respond(
            req, cache_key, etag, build_payload
        )
    except (LosReadModelUnavailableError, LosSchemaError) as error:
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception("LOS facts endpoint failed")
        return json_response({"error": "Unable to retrieve LOS facts"}, 500)


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

    span_error = validate_range_span(start_date, end_date)
    if span_error is not None:
        return span_error

    include_raw = (req.params.get("includeComparison") or "false").lower()
    if include_raw not in {"true", "false", "1", "0"}:
        return json_response(
            {"error": "includeComparison must be true or false"},
            400,
        )
    include_comparison = include_raw in {"true", "1"}
    comparison_basis = req.params.get("lyComparisonBasis") or "sameDate"
    if comparison_basis not in VALID_LY_COMPARISONS:
        return json_response(
            {
                "error": "Invalid lyComparisonBasis",
                "allowedValues": ["sameDate", "sameWeekday"],
            },
            400,
        )

    comparison_mode = req.params.get("comparisonMode") or "final"
    if comparison_mode not in {"final", "spit"}:
        return json_response(
            {
                "error": "Invalid comparisonMode",
                "allowedValues": ["final", "spit"],
            },
            400,
        )

    comparison_start = comparison_end = None
    comparison_cutoff = None
    if include_comparison:
        comparison_start = shift_cost_comparison_date(
            start_date,
            comparison_basis,
        )
        comparison_end = shift_cost_comparison_date(
            end_date,
            comparison_basis,
        )
        if comparison_mode == "spit":
            comparison_cutoff = shift_cost_comparison_date(
                datetime.now(ZoneInfo("Europe/Stockholm")).date(),
                comparison_basis,
            )

    try:
        publication_version = fetch_cost_publication_version()
    except Exception:
        logging.exception("Cost publication lookup failed")
        return json_response({"error": "Unable to retrieve cost data"}, 500)

    spit_publication = None
    if include_comparison and comparison_mode == "spit":
        try:
            spit_publication = fetch_supplement_status().get("runId")
        except Exception:
            # The adjustment lookup below will report SPIT as unavailable. Keep
            # LY Final/current facts usable instead of turning a comparison-only
            # dependency into a page-level failure.
            logging.exception("Cost SPIT publication lookup failed")

    identity = "|".join([
        str(COST_DATA_RESPONSE_SCHEMA_VERSION),
        str(publication_version),
        start_date.isoformat(),
        end_date.isoformat(),
        comparison_basis if include_comparison else "none",
        comparison_mode if include_comparison else "none",
        comparison_cutoff.isoformat() if comparison_cutoff else "none",
        str(spit_publication) if spit_publication is not None else "none",
        comparison_start.isoformat() if comparison_start else "none",
        comparison_end.isoformat() if comparison_end else "none",
    ])
    etag = content_etag("costdata", identity)
    cache_key = (
        COST_DATA_RESPONSE_SCHEMA_VERSION,
        publication_version,
        start_date,
        end_date,
        comparison_basis if include_comparison else None,
        comparison_mode if include_comparison else None,
        comparison_cutoff,
        spit_publication,
        comparison_start,
        comparison_end,
    )

    def build_payload():
        if include_comparison:
            range_results = fetch_cost_data_ranges(
                (
                    ("current", start_date, end_date),
                    ("comparison", comparison_start, comparison_end),
                ),
                publication_version=publication_version,
            )
            datasets, row_counts = range_results["current"]
            comparison_datasets, comparison_row_counts = (
                range_results["comparison"]
            )
            spit_adjustments = (
                fetch_cost_spit_adjustments(
                    comparison_start,
                    comparison_end,
                    comparison_cutoff,
                )
                if comparison_mode == "spit"
                else None
            )
        else:
            datasets, row_counts = fetch_cost_data(
                start_date,
                end_date,
                publication_version=publication_version,
            )
            comparison_datasets = comparison_row_counts = None
            spit_adjustments = None

        # The GOP statement is computed entirely from the saved Cost Input
        # rulebook. Preserve the existing partial-failure behaviour: facts can
        # still be shown when that lookup is temporarily unavailable, but do not
        # retain that degraded body in the server response cache.
        cacheable = True
        try:
            cost_settings = fetch_all_cost_settings(
                publication_version=publication_version,
            )
        except Exception:
            logging.exception(
                "Cost settings lookup failed for the cost data endpoint"
            )
            cost_settings = {}
            cacheable = False

        hotels = sorted(
            {
                row["hotelName"]
                for rows in datasets.values()
                for row in rows
                if row.get("hotelName")
            }
        )

        payload = {
            "parameters": {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
            },
            "publicationVersion": publication_version,
            "rowCounts": row_counts,
            "hotels": hotels,
            "data": datasets,
            "costSettings": cost_settings,
        }
        if include_comparison:
            payload["comparison"] = {
                "parameters": {
                    "startDate": comparison_start.isoformat(),
                    "endDate": comparison_end.isoformat(),
                    "lyComparisonBasis": comparison_basis,
                    "mode": comparison_mode,
                },
                "rowCounts": comparison_row_counts,
                "data": comparison_datasets,
            }
            if comparison_mode == "spit":
                payload["comparison"]["spit"] = {
                    "available": spit_adjustments["available"],
                    "cutoffDate": comparison_cutoff.isoformat(),
                    "adjustments": spit_adjustments["rows"],
                }
        return payload, cacheable

    try:
        return versioned_cost_response(
            req,
            cache_key,
            etag,
            build_payload,
        )
    except Exception:
        logging.exception("Cost data endpoint failed")
        return json_response({"error": "Unable to retrieve cost data"}, 500)


@app.route(
    route="costdata/properties",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def cost_settings_hotels(req: func.HttpRequest) -> func.HttpResponse:
    try:
        # The property list changes once a day, when CostDataTimer runs, yet it
        # was the only cost route declaring itself uncacheable - its siblings
        # already send max-age 300 (sources) and 120 (rates, agencies). An
        # operator who has just imported a new hotel still sees it immediately:
        # the Cost Input page requests this with cache "reload" after an import.
        return json_response(
            {"data": list_cost_settings_hotels()},
            headers={"Cache-Control": "private, max-age=300"},
        )
    except CostSettingsSchemaError as error:
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception("Cost settings hotel endpoint failed")
        return json_response({"error": "Unable to retrieve properties"}, 500)


@app.route(
    route="costdata/sources/{enterprise_id}",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def cost_settings_sources(req: func.HttpRequest) -> func.HttpResponse:
    """Rates, channels and room categories for the Cost Input pickers."""
    enterprise_id = (req.route_params.get("enterprise_id") or "").strip()
    if not enterprise_id:
        return json_response({"error": "Enterprise ID is required"}, 400)
    try:
        return compressed_json_response(
            req,
            {"data": fetch_cost_sources(enterprise_id)},
            headers={"Cache-Control": "private, max-age=300"},
        )
    except CostSourceUnavailableError as error:
        # The source column names could not be resolved. Surface the detail:
        # it names the table and the candidates tried, which is exactly what an
        # operator needs to fix the mapping.
        logging.warning("Cost source lookup unavailable: %s", error)
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception(
            "Cost source endpoint failed enterprise_id=%s", enterprise_id
        )
        return json_response({"error": "Unable to retrieve property source data"}, 500)


def _origin_filter(req: func.HttpRequest):
    """The repeatable ?origin= parameter, or None for "every origin".

    An empty list and "no filter" are different questions and must not collapse
    into each other: an origin group that has picked nothing yet must not be
    offered every rate in the property as if it had.
    """
    origins = [
        value.strip()
        for value in req.params.get("origins", "").split(",")
        if value.strip()
    ]
    return origins or None


@app.route(
    route="costdata/agencies/{enterprise_id}",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def cost_settings_agencies(req: func.HttpRequest) -> func.HttpResponse:
    """Travel agencies matching a case-insensitive "contains" search."""
    enterprise_id = (req.route_params.get("enterprise_id") or "").strip()
    if not enterprise_id:
        return json_response({"error": "Enterprise ID is required"}, 400)
    try:
        agencies = list_travel_agencies(
            enterprise_id,
            search=req.params.get("search", ""),
            origins=_origin_filter(req),
        )
        return compressed_json_response(
            req,
            {"data": agencies},
            headers={"Cache-Control": "private, max-age=120"},
        )
    except CostSourceUnavailableError as error:
        logging.warning("Travel agency lookup unavailable: %s", error)
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception(
            "Travel agency endpoint failed enterprise_id=%s", enterprise_id
        )
        return json_response({"error": "Unable to search travel agencies"}, 500)


@app.route(
    route="costdata/rates/{enterprise_id}",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def cost_settings_rates(req: func.HttpRequest) -> func.HttpResponse:
    """Rates that reservations under the given filters were actually sold on."""
    enterprise_id = (req.route_params.get("enterprise_id") or "").strip()
    if not enterprise_id:
        return json_response({"error": "Enterprise ID is required"}, 400)
    try:
        payload = list_matching_rates(
            enterprise_id,
            origins=_origin_filter(req),
            agencySearch=req.params.get("agency", ""),
        )
        return compressed_json_response(
            req,
            {"data": payload},
            headers={"Cache-Control": "private, max-age=120"},
        )
    except CostSourceUnavailableError as error:
        logging.warning("Matching rate lookup unavailable: %s", error)
        return json_response({"error": str(error)}, 503)
    except Exception:
        logging.exception(
            "Matching rate endpoint failed enterprise_id=%s", enterprise_id
        )
        return json_response({"error": "Unable to retrieve matching rates"}, 500)


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
            # The rulebook is the largest payload on the Cost Input critical
            # path and was the only one of the six cost routes still sending it
            # uncompressed. It is key-per-object JSON with one repeated key set
            # per rule row, which is the shape that compresses roughly 10:1.
            return compressed_json_response(
                req,
                {"data": fetch_cost_settings(enterprise_id, hotel_name)},
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
    except Exception as error:
        logging.exception("Cost settings endpoint failed enterprise_id=%s", enterprise_id)
        action = "retrieve" if req.method == "GET" else "save"
        # A bare "Unable to save cost settings" gave an operator nothing to act
        # on and nothing to report. Surface the SQLSTATE and, for the failure
        # modes that are actually the user's to fix, say what to change. The
        # full exception stays in the logs.
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate == "23505":
            detail = (
                "A uniqueness rule rejected these settings (SQLSTATE 23505). "
                "This usually means the database is missing migration 013, which "
                "allows one cleaning row per room category AND occupancy."
            )
        elif sqlstate == "23514":
            detail = (
                "A value fell outside an allowed range (SQLSTATE 23514). Check "
                "percentages are 0-100 and costs are not negative."
            )
        elif sqlstate == "42703":
            detail = (
                "The database is missing a column this version expects "
                "(SQLSTATE 42703). Apply the pending migrations in sql/migrations."
            )
        elif sqlstate:
            detail = f"The database rejected the request (SQLSTATE {sqlstate})."
        else:
            detail = f"Unexpected {type(error).__name__}."
        return json_response({"error": f"Unable to {action} cost settings. {detail}"}, 500)


@app.function_name(name="CostDataImport")
@app.route(
    route="costdata/import",
    methods=["POST"],
    # ANONYMOUS, matching every other costdata route.
    #
    # This was FUNCTION on the premise that "the Function App answers on its own
    # public hostname - Static Web Apps is a proxy, not a gate". That premise no
    # longer holds: the app is a Static Web Apps LINKED BACKEND, so App Service
    # Authentication fronts it and a direct request to the Function App's own
    # hostname is refused before the route is reached at all - it comes back as
    # {"code":400,"message":"Login not supported for provider azureStaticWebApps"}.
    # The site itself is behind Static Web Apps password protection on top of that.
    #
    # So the key was not the gate; it was just the reason the operator could not
    # use the button. Two layers still stand, and they are the same two that
    # already guard costdata/settings - a PUT that rewrites every cost figure for
    # every hotel, and which has always been ANONYMOUS. This import is idempotent
    # upserts against the same data the nightly timer rewrites anyway, so it is the
    # less consequential of the two by some distance.
    #
    # What would invalidate this: unlinking the backend from Static Web Apps, or
    # disabling App Service Authentication on the Function App. Either one leaves
    # every costdata route open, not only this one, so the check belongs to the
    # deployment rather than to this decorator - see COST_RESERVATION_MIX_DEPLOYMENT.md.
    #
    # los/import and supplement/import are deliberately left as FUNCTION. Nothing
    # in the application calls them, so nothing is blocked by their staying shut.
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
        known_datasets = dataset_names()
        if dataset != "all" and dataset not in known_datasets:
            return json_response(
                {
                    "error": (
                        f"Unknown dataset '{dataset}'. "
                        f"Allowed: {sorted(known_datasets)}"
                    )
                },
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
    # Which half of the detail to build. The figures come from the published
    # read model and are ready in milliseconds; the pickup curves are rebuilt
    # from reservation lifecycle in the source database and are the slow half.
    # Asking for them separately lets the dialog fill in as the figures land
    # rather than holding an empty panel until the curves finish. Omitted means
    # both, which is what the endpoint has always returned.
    include = (req.params.get("include") or "all").strip().lower()
    # Lookback window for the pickup curve, in days before the stay date.
    # Omitted or "all" means the complete history back to the first booking -
    # deliberately not a large sentinel number, so nothing clips it.
    days_before_raw = (req.params.get("daysBeforeStay") or "").strip().lower()
    days_before_stay = None
    if days_before_raw and days_before_raw != "all":
        try:
            days_before_stay = int(days_before_raw)
        except ValueError:
            return json_response(
                {"error": "daysBeforeStay must be a whole number of days or 'all'"},
                400,
            )
        if days_before_stay < 1:
            return json_response(
                {"error": "daysBeforeStay must be at least 1"},
                400,
            )
    if not hotel_code or stay_date is None:
        return json_response(
            {"error": "hotelCode and a YYYY-MM-DD stayDate are required"},
            400,
        )
    try:
        started_at = perf_counter()
        payload = fetch_supplement_detail(
            hotel_code, stay_date, category, ly_basis, inventory_basis,
            days_before_stay, include,
        )
        request_key = "|".join([
            hotel_code, stay_date.isoformat(), category or "", ly_basis,
            inventory_basis, days_before_raw or "all", include,
        ])
        response = supplement_cached_response(req, payload, request_key)
        logging.info(
            "Supplement detail served run_id=%s include=%s elapsed_ms=%.1f",
            payload.get("runId"), include, (perf_counter() - started_at) * 1000,
        )
        return response
    except ValueError as error:
        return json_response({"error": str(error)}, 400)
    except (SupplementUnavailableError, SupplementSchemaError) as error:
        return json_response({"error": str(error)}, 503)
    except Exception as error:
        logging.exception("Supplement detail endpoint failed")
        # A bare "Unable to retrieve Supplement detail" tells whoever reports it
        # nothing, and tells whoever has to fix it nothing either - the only
        # record is a stack trace nobody has in front of them. The class and the
        # SQLSTATE are enough to place the failure and carry no query text, no
        # parameters, and no data, which is the same line the schema services
        # already draw.
        return json_response(
            {
                "error": "Unable to retrieve Supplement detail",
                "failure": type(error).__name__,
                "sqlstate": getattr(error, "sqlstate", None),
            },
            500,
        )


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
