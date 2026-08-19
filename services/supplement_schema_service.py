import logging

from pathlib import Path
from threading import Lock

from cost_database import cost_pool
from services.cost_schema_service import ensure_cost_settings_schema
from services.schema_bootstrap import migrations_are_current


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
    (
        "006_unified_hotels",
        APP_ROOT / "sql" / "migrations" / "006_unified_hotels.sql",
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

        # Supplement publishes into the shared hotel dimension. Ensure the
        # Cost Data publication pointer that names changes to that dimension is
        # present before a sync can update it.
        ensure_cost_settings_schema()
        with cost_pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtext(%s))",
                    ("functions.application_schema",),
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
                        ("functions.application_schema",),
                    )

        _schema_ready = True
