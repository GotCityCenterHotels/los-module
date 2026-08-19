import logging

from pathlib import Path
from threading import Lock

from cost_database import cost_pool
from services.schema_bootstrap import migrations_are_current


APP_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_NAME = "005_import_jobs"
MIGRATION_PATH = APP_ROOT / "sql" / "migrations" / "005_import_jobs.sql"

_schema_ready = False
_schema_lock = Lock()


class ImportJobSchemaError(RuntimeError):
    pass


def ensure_import_job_schema():
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
                if migrations_are_current(cursor, [MIGRATION_NAME]):
                    _schema_ready = True
                    return

        with cost_pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtext(%s))",
                    ("functions.import_job_schema",),
                )
                try:
                    cursor.execute("CREATE SCHEMA IF NOT EXISTS functions")
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS functions.schema_migrations (
                            migration_name text PRIMARY KEY,
                            applied_at timestamptz NOT NULL DEFAULT now()
                        )
                    """)
                    cursor.execute(
                        "SELECT 1 FROM functions.schema_migrations "
                        "WHERE migration_name = %s",
                        (MIGRATION_NAME,),
                    )
                    if cursor.fetchone() is None:
                        cursor.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
                        logging.info(
                            "Applied import job migration name=%s path=%s",
                            MIGRATION_NAME,
                            MIGRATION_PATH,
                        )
                except Exception as error:
                    connection.rollback()
                    sqlstate = getattr(error, "sqlstate", None) or "unknown"
                    logging.exception(
                        "Import job schema bootstrap failed sqlstate=%s", sqlstate
                    )
                    raise ImportJobSchemaError(
                        f"Import job database schema is not ready (SQLSTATE {sqlstate})"
                    ) from error
                finally:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        ("functions.import_job_schema",),
                    )

        _schema_ready = True
