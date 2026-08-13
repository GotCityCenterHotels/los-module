import os

import psycopg
from psycopg.rows import dict_row


def _setting(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    if default is not None:
        return default
    raise KeyError(names[0])


def get_export_connection():
    # Database B: integration_db. All export, LOS, cost-source, and Supplement
    # access is read-only at both the role and session level.
    return psycopg.connect(
        host=_setting(
            "INTEGRATION_DB_HOST",
            "EXPORT_POSTGRES_HOST",
            "DB_HOST",
        ),
        dbname=_setting(
            "INTEGRATION_DB_NAME",
            "EXPORT_POSTGRES_DB",
            "DB_NAME",
            default="integration_db",
        ),
        user=_setting(
            "INTEGRATION_DB_USER",
            "EXPORT_POSTGRES_USER",
            "DB_USER",
        ),
        password=_setting(
            "INTEGRATION_DB_PASSWORD",
            "EXPORT_POSTGRES_PASSWORD",
            "DB_PASSWORD",
        ),
        port=int(
            _setting(
                "INTEGRATION_DB_PORT",
                "EXPORT_POSTGRES_PORT",
                "DB_PORT",
                default="5432",
            )
        ),
        sslmode=_setting(
            "INTEGRATION_DB_SSLMODE",
            "EXPORT_POSTGRES_SSLMODE",
            "DB_SSLMODE",
            default="require",
        ),
        options="-c default_transaction_read_only=on -c statement_timeout=300000",
        row_factory=dict_row,
    )


def get_import_connection():
    # Database A: the writable PostgreSQL application database.
    app_db_name = _setting("COST_DB_NAME", "POSTGRES_DB")
    if app_db_name.lower() == "integration_db":
        raise RuntimeError("Database A cannot be integration_db")
    return psycopg.connect(
        host=_setting("COST_DB_HOST", "POSTGRES_HOST"),
        dbname=app_db_name,
        user=_setting("COST_DB_USER", "POSTGRES_USER"),
        password=_setting(
            "COST_DB_PASSWORD",
            "POSTGRES_PASSWORD",
        ),
        port=int(
            _setting(
                "COST_DB_PORT",
                "POSTGRES_PORT",
                default="5432",
            )
        ),
        sslmode=_setting(
            "COST_DB_SSLMODE",
            "POSTGRES_SSLMODE",
            default="require",
        ),
    )
