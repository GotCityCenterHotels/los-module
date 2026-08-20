import logging

from pathlib import Path
from threading import Lock

from cost_database import cost_pool
from services.cost_schema_service import ensure_cost_settings_schema
from services.import_job_schema_service import ensure_import_job_schema
from services.schema_bootstrap import migrations_are_current


APP_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = (
    (
        "008_los_read_model",
        APP_ROOT / "sql" / "migrations" / "008_los_read_model.sql",
    ),
    # 005 is owned by import_job_schema_service and is listed here as well, the
    # same way 006_unified_hotels is shared by the cost and supplement services.
    #
    # It has to be, because 009 below is an ALTER TABLE against
    # functions.import_jobs and 005 is what creates it. On an established
    # database that is invisible - the table has existed for months. On a rebuilt
    # one, whichever request arrives first applies its own list, so a LOS request
    # ahead of any import request ran 009 against a table that did not exist yet
    # and answered 503 on every subsequent LOS call until something happened to
    # touch the import-jobs schema. Declaring the dependency is what makes the
    # order independent of which page someone opens first.
    (
        "005_import_jobs",
        APP_ROOT / "sql" / "migrations" / "005_import_jobs.sql",
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

        # Fast path: two round trips, no advisory lock. A migration name is
        # recorded in the transaction that applies it, so a worker that can see
        # every expected name knows the schema is current without coordinating.
        # The lock below is cluster-wide, so taking it unconditionally made the
        # parallel requests one page load fires serialize behind each other.
        with cost_pool.connection() as connection:
            with connection.cursor() as cursor:
                if migrations_are_current(
                    cursor, [name for name, _ in MIGRATIONS]
                ):
                    _schema_ready = True
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
