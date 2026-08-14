import json
import logging
import uuid

from datetime import date, datetime

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cost_database import cost_pool
from services.import_job_schema_service import ensure_import_job_schema


ACTIVE_STATUSES = ("queued", "running", "retrying")
TERMINAL_STATUSES = ("succeeded", "failed")


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Unable to encode {type(value).__name__}")


def _json_value(value):
    return json.loads(json.dumps(value, default=_json_default))


def _job_payload(row):
    if row is None:
        return None
    return {
        "jobId": str(row["job_id"]),
        "jobType": row["job_type"],
        "operation": row["operation"],
        "status": row["status"],
        "attemptCount": row["attempt_count"],
        "createdAt": row["created_at"].isoformat(),
        "startedAt": row["started_at"].isoformat() if row["started_at"] else None,
        "updatedAt": row["updated_at"].isoformat(),
        "finishedAt": row["finished_at"].isoformat() if row["finished_at"] else None,
        "result": row["result"],
        "error": row["error_message"],
    }


def create_import_job(job_type, operation, payload):
    if job_type not in {"cost", "supplement", "los"}:
        raise ValueError("job_type must be cost, supplement, or los")
    ensure_import_job_schema()
    if job_type == "los":
        # Existing databases need migration 009 before inserting this family.
        from services.los_schema_service import ensure_los_schema

        ensure_los_schema()
    job_id = uuid.uuid4()
    normalized_payload = _json_value(payload)

    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            # Serialize creation per import family so two HTTP/timer invocations
            # cannot both pass the active-job check.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"functions.import_jobs.{job_type}",),
            )
            cursor.execute("""
                SELECT * FROM functions.import_jobs
                WHERE job_type = %s
                  AND status = ANY(%s)
                ORDER BY created_at
                LIMIT 1
            """, (job_type, list(ACTIVE_STATUSES)))
            existing = cursor.fetchone()
            if existing is not None:
                return _job_payload(existing), False
            cursor.execute("""
                INSERT INTO functions.import_jobs (
                    job_id, job_type, operation, payload, status
                ) VALUES (%s, %s, %s, %s, 'queued')
                RETURNING *
            """, (job_id, job_type, operation, Jsonb(normalized_payload)))
            return _job_payload(cursor.fetchone()), True


def get_import_job(job_id):
    ensure_import_job_schema()
    try:
        normalized_id = uuid.UUID(str(job_id))
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid import job ID") from error
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT * FROM functions.import_jobs WHERE job_id = %s",
                (normalized_id,),
            )
            return _job_payload(cursor.fetchone())


def claim_import_job(job_id, dequeue_count):
    ensure_import_job_schema()
    with cost_pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""
                UPDATE functions.import_jobs
                SET status = 'running',
                    attempt_count = %s,
                    started_at = coalesce(started_at, now()),
                    updated_at = now(),
                    finished_at = NULL,
                    error_message = NULL
                WHERE job_id = %s
                  AND (
                    status IN ('queued', 'retrying')
                    OR (status = 'running' AND attempt_count < %s)
                  )
                RETURNING *
            """, (dequeue_count, uuid.UUID(str(job_id)), dequeue_count))
            return cursor.fetchone()


def complete_import_job(job_id, result):
    ensure_import_job_schema()
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE functions.import_jobs
                SET status = 'succeeded', result = %s, error_message = NULL,
                    updated_at = now(), finished_at = now()
                WHERE job_id = %s AND status = 'running'
            """, (Jsonb(_json_value(result)), uuid.UUID(str(job_id))))


def fail_import_job(job_id, error, will_retry):
    ensure_import_job_schema()
    status = "retrying" if will_retry else "failed"
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE functions.import_jobs
                SET status = %s, error_message = %s, updated_at = now(),
                    finished_at = CASE WHEN %s THEN NULL ELSE now() END
                WHERE job_id = %s AND status = 'running'
            """, (
                status,
                str(error).splitlines()[0][:2000],
                will_retry,
                uuid.UUID(str(job_id)),
            ))
    logging.warning(
        "Import job attempt failed job_id=%s retry=%s error_type=%s",
        job_id,
        will_retry,
        type(error).__name__,
    )


def log_pool_stats(job_id):
    stats = cost_pool.get_stats()
    logging.info(
        "Import job pool stats job_id=%s pool_size=%s pool_available=%s "
        "requests_waiting=%s requests_errors=%s",
        job_id,
        stats.get("pool_size"),
        stats.get("pool_available"),
        stats.get("requests_waiting"),
        stats.get("requests_errors"),
    )
