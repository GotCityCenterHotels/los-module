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
    # 011 is deliberately excluded: it drops functions.cost_fixed_lines and is
    # applied by hand, matching how 007 is handled. Destructive DDL must not run
    # from an ordinary page request.
    (
        "012_cleaning_occupancy",
        APP_ROOT / "sql" / "migrations" / "012_cleaning_occupancy.sql",
    ),
    (
        "013_cleaning_occupancy_unique_fix",
        APP_ROOT / "sql" / "migrations" / "013_cleaning_occupancy_unique_fix.sql",
    ),
    (
        "014_franchise_and_distribution_tree",
        APP_ROOT / "sql" / "migrations" / "014_franchise_and_distribution_tree.sql",
    ),
    (
        "015_bed_types_and_cleaning_inheritance",
        APP_ROOT / "sql" / "migrations" / "015_bed_types_and_cleaning_inheritance.sql",
    ),
    (
        "016_cost_reservation_mix",
        APP_ROOT / "sql" / "migrations" / "016_cost_reservation_mix.sql",
    ),
)

_schema_ready = False
_schema_lock = Lock()


class CostSettingsSchemaError(RuntimeError):
    pass


def _read_sql(path):
    return path.read_text(encoding="utf-8")


def _pending_migrations(cursor):
    """Migrations not yet recorded, in one round trip.

    Checking each of them with its own SELECT cost one network round trip per
    migration on every cold worker, all to discover that nothing needs doing.
    Returns None when the bookkeeping table itself is missing, which means the
    full bootstrap has to run.
    """
    cursor.execute("SELECT to_regclass('functions.schema_migrations')")
    if cursor.fetchone()[0] is None:
        return None
    cursor.execute(
        "SELECT migration_name FROM functions.schema_migrations "
        "WHERE migration_name = ANY(%s)",
        ([name for name, _ in MIGRATIONS],),
    )
    applied = {row[0] for row in cursor.fetchall()}
    return [name for name, _ in MIGRATIONS if name not in applied]


def ensure_cost_settings_schema():
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        if _schema_ready:
            return

        # Fast path: one round trip, no advisory lock. A worker that finds the
        # schema already current never contends with another worker's
        # migration, and never pays the lock's own round trip pair.
        with cost_pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass('functions.cost_property_settings')"
                )
                settings_ready = cursor.fetchone()[0] is not None
                if settings_ready and _pending_migrations(cursor) == []:
                    _schema_ready = True
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

                    # Re-read under the lock: another worker may have applied
                    # everything between the fast path above and this point.
                    pending = _pending_migrations(cursor)
                    outstanding = (
                        {name for name, _ in MIGRATIONS}
                        if pending is None
                        else set(pending)
                    )
                    for migration_name, migration_path in MIGRATIONS:
                        if migration_name not in outstanding:
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
