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
    from services.cost_schema_service import BASE_SCHEMA_PATH, MIGRATIONS

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
            for index_name, definition in cursor.fetchall():
                print(f"  {index_name}: {definition}")
                if "occupancy" not in definition and "UNIQUE" in definition.upper():
                    print(
                        "FAIL  a unique index without occupancy still exists; "
                        "multi-occupancy cleaning rows cannot be saved",
                        file=sys.stderr,
                    )
                    return 1

    print("\nAll migrations applied cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
