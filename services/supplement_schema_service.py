import logging

from pathlib import Path
from threading import Lock

from cost_database import cost_pool


APP_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = (
    (
        "003_supplement_read_model",
        APP_ROOT / "sql" / "migrations" / "003_supplement_read_model.sql",
    ),
    (
        "004_supplement_lifecycle_ids",
        APP_ROOT / "sql" / "migrations" / "004_supplement_lifecycle_ids.sql",
    ),
)

_schema_ready = False
_schema_lock = Lock()


class SupplementSchemaError(RuntimeError):
    pass


def ensure_supplement_schema():
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        if _schema_ready:
            return

        with cost_pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtext(%s))",
                    ("functions.supplement_schema",),
                )
                try:
                    cursor.execute("CREATE SCHEMA IF NOT EXISTS functions")
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS functions.schema_migrations (
                            migration_name text PRIMARY KEY,
                            applied_at timestamptz NOT NULL DEFAULT now()
                        )
                    """)
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
                            "Applied Supplement migration name=%s path=%s",
                            migration_name,
                            migration_path,
                        )
                except Exception as error:
                    connection.rollback()
                    sqlstate = getattr(error, "sqlstate", None) or "unknown"
                    logging.exception("Supplement schema bootstrap failed sqlstate=%s", sqlstate)
                    raise SupplementSchemaError(
                        f"Supplement database schema is not ready (SQLSTATE {sqlstate})"
                    ) from error
                finally:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        ("functions.supplement_schema",),
                    )

        _schema_ready = True
