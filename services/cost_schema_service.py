import logging

from pathlib import Path
from threading import Lock

from cost_database import cost_pool


APP_ROOT = Path(__file__).resolve().parent.parent
BASE_SCHEMA_PATH = APP_ROOT / "sql" / "tables" / "cost_input_settings.sql"
MIGRATIONS = (
    (
        "001_cost_settings_enterprise_text",
        APP_ROOT / "sql" / "migrations" / "001_cost_settings_enterprise_text.sql",
    ),
    (
        "002_cost_properties",
        APP_ROOT / "sql" / "migrations" / "002_cost_properties.sql",
    ),
    (
        "006_unified_hotels",
        APP_ROOT / "sql" / "migrations" / "006_unified_hotels.sql",
    ),
    (
        "010_cost_fact_tables",
        APP_ROOT / "sql" / "migrations" / "010_cost_fact_tables.sql",
    ),
)

_schema_ready = False
_schema_lock = Lock()


class CostSettingsSchemaError(RuntimeError):
    pass


def _read_sql(path):
    return path.read_text(encoding="utf-8")


def ensure_cost_settings_schema():
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        if _schema_ready:
            return

        with cost_pool.connection() as connection:
            with connection.cursor() as cursor:
                # A session lock coordinates migrations across Function workers.
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtext(%s))",
                    ("functions.application_schema",),
                )
                try:
                    cursor.execute(
                        "SELECT to_regclass('functions.schema_migrations')"
                    )
                    migration_table_exists = cursor.fetchone()[0] is not None
                    if not migration_table_exists:
                        cursor.execute("CREATE SCHEMA IF NOT EXISTS functions")
                        cursor.execute("""
                            CREATE TABLE IF NOT EXISTS functions.schema_migrations (
                                migration_name text PRIMARY KEY,
                                applied_at timestamptz NOT NULL DEFAULT now()
                            )
                        """)
                    cursor.execute(
                        "SELECT to_regclass('functions.cost_property_settings')"
                    )
                    settings_table_exists = cursor.fetchone()[0] is not None
                    if not settings_table_exists:
                        cursor.execute(_read_sql(BASE_SCHEMA_PATH))
                        logging.info(
                            "Applied fresh cost settings schema path=%s",
                            BASE_SCHEMA_PATH,
                        )

                    for migration_name, migration_path in MIGRATIONS:
                        cursor.execute(
                            "SELECT 1 FROM functions.schema_migrations "
                            "WHERE migration_name = %s",
                            (migration_name,),
                        )
                        if cursor.fetchone() is not None:
                            continue
                        cursor.execute(_read_sql(migration_path))
                        logging.info(
                            "Applied cost settings migration name=%s path=%s",
                            migration_name,
                            migration_path,
                        )
                except Exception as error:
                    # A failed multi-statement migration leaves PostgreSQL's
                    # transaction aborted. Roll it back before releasing the
                    # session-level advisory lock back to the connection pool.
                    connection.rollback()
                    sqlstate = getattr(error, "sqlstate", None) or "unknown"
                    error_type = type(error).__name__
                    error_detail = str(error).splitlines()[0][:240]
                    logging.exception(
                        "Cost settings schema bootstrap failed sqlstate=%s error_type=%s",
                        sqlstate,
                        error_type,
                    )
                    raise CostSettingsSchemaError(
                        f"Cost settings database schema is not ready (SQLSTATE {sqlstate}). "
                        f"{error_type}: {error_detail}"
                    ) from error
                finally:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        ("functions.application_schema",),
                    )

        _schema_ready = True
