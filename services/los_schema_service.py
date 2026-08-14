import logging

from pathlib import Path
from threading import Lock

from cost_database import cost_pool
from services.cost_schema_service import ensure_cost_settings_schema
from services.import_job_schema_service import ensure_import_job_schema


APP_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = (
    (
        "008_los_read_model",
        APP_ROOT / "sql" / "migrations" / "008_los_read_model.sql",
    ),
    (
        "009_import_jobs_los",
        APP_ROOT / "sql" / "migrations" / "009_import_jobs_los.sql",
    ),
)

_schema_ready = False
_schema_lock = Lock()


class LosSchemaError(RuntimeError):
    pass


def ensure_los_schema():
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        if _schema_ready:
            return

        # The LOS fact foreign key targets the unified hotel dimension.
        ensure_cost_settings_schema()
        ensure_import_job_schema()
        with cost_pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtext(%s))",
                    ("functions.application_schema",),
                )
                try:
                    for migration_name, migration_path in MIGRATIONS:
                        cursor.execute(
                            "SELECT 1 FROM functions.schema_migrations "
                            "WHERE migration_name = %s",
                            (migration_name,),
                        )
                        if cursor.fetchone() is not None:
                            continue
                        cursor.execute(migration_path.read_text(encoding="utf-8"))
                        logging.info(
                            "Applied LOS migration name=%s path=%s",
                            migration_name,
                            migration_path,
                        )
                except Exception as error:
                    connection.rollback()
                    sqlstate = getattr(error, "sqlstate", None) or "unknown"
                    logging.exception(
                        "LOS schema bootstrap failed sqlstate=%s", sqlstate
                    )
                    raise LosSchemaError(
                        f"LOS database schema is not ready (SQLSTATE {sqlstate})"
                    ) from error
                finally:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        ("functions.application_schema",),
                    )

        _schema_ready = True
