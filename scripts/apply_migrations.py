"""Apply the cost schema to a PostgreSQL database and report what it produced.

Written for CI against a throwaway Postgres, so that migrations are actually
EXECUTED somewhere before they reach a real database. Parsing SQL only proves
the grammar is valid - it cannot catch a type error such as comparing name[] to
text[], which is exactly how a broken migration reached production.

Usage:
    python scripts/apply_migrations.py                # uses COST_DB_* / POSTGRES_*
    python scripts/apply_migrations.py --dsn "postgres://..."

Exits non-zero if any statement fails.
"""

import argparse
import os
import sys

from pathlib import Path

import psycopg


REPO_ROOT = Path(__file__).resolve().parent.parent


def build_dsn(explicit):
    if explicit:
        return explicit
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("COST_DB_HOST", os.environ.get("POSTGRES_HOST", "localhost")),
        port=os.environ.get("COST_DB_PORT", os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ.get("COST_DB_NAME", os.environ.get("POSTGRES_DB", "postgres")),
        user=os.environ.get("COST_DB_USER", os.environ.get("POSTGRES_USER", "postgres")),
        password=os.environ.get("COST_DB_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", help="Full connection string; overrides env vars")
    arguments = parser.parse_args()

    # Imported here so a missing app setting cannot break --help.
    sys.path.insert(0, str(REPO_ROOT))

    # cost_schema_service imports cost_database, which builds a ConnectionPool
    # at module import and raises KeyError on any missing setting. This script
    # never uses that pool - it connects with its own DSN - so placeholders are
    # enough to get through the import when running with --dsn alone.
    for name, placeholder in (
        ("COST_DB_NAME", "placeholder"),
        ("COST_DB_HOST", "localhost"),
        ("COST_DB_USER", "placeholder"),
        ("COST_DB_PASSWORD", "placeholder"),
    ):
        os.environ.setdefault(name, placeholder)

    from services.cost_schema_service import BASE_SCHEMA_PATH, MIGRATIONS

    # cost_database builds a ConnectionPool at import, which this script never
    # uses. Left open, its worker threads outlive main() and print "couldn't
    # stop thread" warnings over the real output.
    try:
        from cost_database import cost_pool

        cost_pool.close()
    except Exception:  # pragma: no cover - only affects log noise
        pass

    steps = [("base schema", BASE_SCHEMA_PATH)] + list(MIGRATIONS)

    with psycopg.connect(build_dsn(arguments.dsn), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS functions")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS functions.schema_migrations (
                    migration_name text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            for name, path in steps:
                sql = Path(path).read_text(encoding="utf-8")
                try:
                    cursor.execute(sql)
                except Exception as error:
                    sqlstate = getattr(error, "sqlstate", "unknown")
                    print(f"FAIL  {name}  (SQLSTATE {sqlstate})", file=sys.stderr)
                    print(f"      {error}", file=sys.stderr)
                    return 1
                print(f"ok    {name}")

            # Re-running must be a no-op: every migration is meant to be
            # idempotent, and the app re-applies any not yet recorded.
            for name, path in steps:
                sql = Path(path).read_text(encoding="utf-8")
                try:
                    cursor.execute(sql)
                except Exception as error:
                    sqlstate = getattr(error, "sqlstate", "unknown")
                    print(
                        f"FAIL  {name} is not idempotent (SQLSTATE {sqlstate})",
                        file=sys.stderr,
                    )
                    print(f"      {error}", file=sys.stderr)
                    return 1
            print("ok    all steps are idempotent")

            cursor.execute("""
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'functions'
                  AND tablename = 'cost_cleaning_categories'
                ORDER BY indexname
            """)
            print("\nfunctions.cost_cleaning_categories indexes:")
            stale_keys = []
            for index_name, definition in cursor.fetchall():
                print(f"  {index_name}: {definition}")
                lowered = definition.lower()
                # The dangerous shape is uniqueness over category_name WITHOUT
                # occupancy. The primary key over cleaning_category_id is unique
                # too and must not be flagged.
                if (
                    "unique" in lowered
                    and "category_name" in lowered
                    and "occupancy" not in lowered
                ):
                    stale_keys.append(index_name)
            if stale_keys:
                print(
                    f"FAIL  {stale_keys} key cleaning on category_name without "
                    "occupancy; multi-occupancy rows cannot be saved",
                    file=sys.stderr,
                )
                return 1

            # Reproduce the save that failed in production: one room category at
            # three occupancies. This is the assertion that would have caught the
            # original bug, and it exercises the schema rather than reading it.
            print("\nfunctional check: one category at three occupancies")
            try:
                cursor.execute("""
                    INSERT INTO functions.hotels (enterprise_id, tenant_key, hotel_name)
                    VALUES ('probe-hotel', 'GCCH', 'Probe Hotel')
                    ON CONFLICT (enterprise_id) DO NOTHING
                """)
                cursor.execute("""
                    INSERT INTO functions.cost_property_settings (enterprise_id, hotel_name)
                    VALUES ('probe-hotel', 'Probe Hotel')
                    ON CONFLICT (enterprise_id) DO NOTHING
                """)
                cursor.executemany(
                    """
                    INSERT INTO functions.cost_cleaning_categories (
                        enterprise_id, category_name, resource_category_id,
                        occupancy, min_guests, max_guests,
                        cleaning_minutes, linen_cost, sort_order
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        ("probe-hotel", "Double Room", "cat-1", occupancy,
                         occupancy, occupancy, 30, 75, index)
                        for index, occupancy in enumerate((1, 2, 3))
                    ],
                )
                cursor.execute("""
                    SELECT count(*) FROM functions.cost_cleaning_categories
                    WHERE enterprise_id = 'probe-hotel'
                """)
                saved = cursor.fetchone()[0]
                if saved != 3:
                    print(f"FAIL  expected 3 rows, stored {saved}", file=sys.stderr)
                    return 1
                print("  ok  stored 3 occupancy rows for one category")
            except Exception as error:
                sqlstate = getattr(error, "sqlstate", "unknown")
                print(
                    f"FAIL  saving multi-occupancy cleaning rows (SQLSTATE {sqlstate})",
                    file=sys.stderr,
                )
                print(f"      {error}", file=sys.stderr)
                return 1
            finally:
                cursor.execute(
                    "DELETE FROM functions.cost_property_settings WHERE enterprise_id = 'probe-hotel'"
                )
                cursor.execute(
                    "DELETE FROM functions.hotels WHERE enterprise_id = 'probe-hotel'"
                )

    print("\nAll migrations applied cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
