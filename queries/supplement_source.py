import os
import re

from contextlib import contextmanager
from datetime import timedelta

from psycopg import sql

from shared.db import get_export_connection


_RELATION_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


def _require_integration_settings():
    required = (
        "INTEGRATION_DB_HOST",
        "INTEGRATION_DB_USER",
        "INTEGRATION_DB_PASSWORD",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Supplement synchronization requires dedicated Database B settings: "
            + ", ".join(missing)
        )
    database_name = os.environ.get("INTEGRATION_DB_NAME", "integration_db")
    if database_name.lower() != "integration_db":
        raise RuntimeError("Supplement Database B must be integration_db")
    return database_name


@contextmanager
def _read_only_source_connection():
    expected_database = _require_integration_settings()
    with get_export_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database() AS database_name, "
                "current_setting('transaction_read_only') AS read_only"
            )
            boundary = cursor.fetchone()
        if boundary["database_name"].lower() != expected_database.lower():
            raise RuntimeError("Supplement source connection opened the wrong database")
        if boundary["read_only"].lower() != "on":
            raise RuntimeError("Supplement source connection is not read-only")
        yield connection


def _source_relation():
    configured = os.environ.get("SUPPLEMENT_SOURCE_RELATION")
    if not configured:
        raise RuntimeError(
            "SUPPLEMENT_SOURCE_RELATION must identify the profiled integration_db projection"
        )
    if not _RELATION_PATTERN.fullmatch(configured):
        raise ValueError("SUPPLEMENT_SOURCE_RELATION must be a schema-qualified identifier")
    schema_name, relation_name = configured.split(".", 1)
    return sql.Identifier(schema_name, relation_name)


def _snapshot_select():
    # This is deliberately a projection-only query. Aggregation and rollups
    # belong in Database A, keeping integration_db work bounded and predictable.
    return sql.SQL("""
        SELECT
            view_date::date AS snapshot_date,
            stay_date::date AS stay_date,
            trim(hotel_code)::text AS hotel_code,
            trim(space_room_name)::text AS space_room_name,
            trim(requested_room_name)::text AS requested_room_name,
            total_assigned_space::numeric AS assigned_rooms,
            sum_price::numeric AS room_revenue,
            total_space::numeric AS total_space,
            space_to_sell::numeric AS space_to_sell
        FROM {}
        WHERE view_date >= %(snapshot_date)s::date
          AND view_date < (%(snapshot_date)s::date + 1)
          AND stay_date >= %(minimum_stay_date)s::date
          AND stay_date < (%(maximum_stay_date)s::date + 1)
    """).format(_source_relation())


def fetch_latest_source_snapshot():
    query = sql.SQL("SELECT max(view_date)::date AS snapshot_date FROM {}").format(
        _source_relation()
    )
    with _read_only_source_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            row = cursor.fetchone()
            return row["snapshot_date"] if row else None


def fetch_source_snapshot_dates(snapshot_from, snapshot_to):
    query = sql.SQL("""
        SELECT DISTINCT view_date::date AS snapshot_date
        FROM {}
        WHERE view_date >= %(snapshot_from)s::date
          AND view_date < (%(snapshot_to)s::date + 1)
        ORDER BY snapshot_date
    """).format(_source_relation())
    with _read_only_source_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, {
                "snapshot_from": snapshot_from,
                "snapshot_to": snapshot_to,
            })
            return [row["snapshot_date"] for row in cursor.fetchall()]


def iter_source_snapshot_batches(
    snapshot_date,
    maximum_stay_date,
    batch_size=5000,
    minimum_stay_date=None,
):
    parameters = {
        "snapshot_date": snapshot_date,
        "minimum_stay_date": minimum_stay_date or snapshot_date - timedelta(days=7),
        "maximum_stay_date": maximum_stay_date,
    }
    with _read_only_source_connection() as connection:
        with connection.cursor(name="supplement_snapshot_export") as cursor:
            cursor.execute(_snapshot_select(), parameters)
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    return
                yield rows


def explain_source_snapshot(snapshot_date, maximum_stay_date):
    parameters = {
        "snapshot_date": snapshot_date,
        "minimum_stay_date": snapshot_date - timedelta(days=7),
        "maximum_stay_date": maximum_stay_date,
    }
    query = sql.SQL("EXPLAIN (ANALYZE, BUFFERS, SETTINGS, SUMMARY, FORMAT JSON) {}")
    query = query.format(_snapshot_select())
    with _read_only_source_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, parameters)
            return next(iter(cursor.fetchone().values()))


def explain_latest_source_snapshot():
    query = sql.SQL("""
        EXPLAIN (ANALYZE, BUFFERS, SETTINGS, SUMMARY, FORMAT JSON)
        SELECT max(view_date)::date AS snapshot_date FROM {}
    """).format(_source_relation())
    with _read_only_source_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            return next(iter(cursor.fetchone().values()))
