import logging

from pathlib import Path
from threading import Lock

from cost_database import cost_pool


APP_ROOT = Path(__file__).resolve().parent.parent
BASE_SCHEMA_PATH = APP_ROOT / "sql" / "tables" / "cost_input_settings.sql"
MIGRATION_PATH = APP_ROOT / "sql" / "migrations" / "001_cost_settings_enterprise_text.sql"
MIGRATION_NAME = "001_cost_settings_enterprise_text"

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
                    ("functions.cost_settings_schema",),
                )
                try:
                    cursor.execute(
                        "SELECT to_regclass('functions.schema_migrations')"
                    )
                    migration_table_exists = cursor.fetchone()[0] is not None
                    migration_applied = False
                    if migration_table_exists:
                        cursor.execute(
                            "SELECT 1 FROM functions.schema_migrations WHERE migration_name = %s",
                            (MIGRATION_NAME,),
                        )
                        migration_applied = cursor.fetchone() is not None

                    if not migration_applied:
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
                        script_path = (
                            MIGRATION_PATH if settings_table_exists else BASE_SCHEMA_PATH
                        )
                        cursor.execute(_read_sql(script_path))
                        cursor.execute(
                            """
                            INSERT INTO functions.schema_migrations (migration_name)
                            VALUES (%s)
                            ON CONFLICT (migration_name) DO NOTHING
                            """,
                            (MIGRATION_NAME,),
                        )
                        logging.info(
                            "Applied cost settings database script path=%s",
                            script_path,
                        )
                except Exception as error:
                    # A failed multi-statement migration leaves PostgreSQL's
                    # transaction aborted. Roll it back before releasing the
                    # session-level advisory lock back to the connection pool.
                    connection.rollback()
                    sqlstate = getattr(error, "sqlstate", None) or "unknown"
                    logging.exception(
                        "Cost settings schema bootstrap failed sqlstate=%s",
                        sqlstate,
                    )
                    raise CostSettingsSchemaError(
                        f"Cost settings database schema is not ready (SQLSTATE {sqlstate}). "
                        "Apply sql/migrations/001_cost_settings_enterprise_text.sql "
                        "with a database owner account."
                    ) from error
                finally:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        ("functions.cost_settings_schema",),
                    )

        _schema_ready = True
