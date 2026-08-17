import os

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row


def _setting(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    if default is not None:
        return default
    raise KeyError(names[0])


# Background jobs (LOS sync, Supplement sync, sql_runner) legitimately run for
# minutes, so the default ceiling stays where it has always been. A page request
# is a different animal: the browser aborts at 40s (frontend/los-api.js) and
# Static Web Apps kills a linked-backend call at ~45s, so a query that outlives
# that is pure waste - it keeps burning an integration_db backend long after the
# client gave up, and an impatient reload leaves several of them running at once.
# HTTP read paths pass their own, tighter ceiling.
DEFAULT_EXPORT_STATEMENT_TIMEOUT_MS = int(
    os.environ.get("EXPORT_STATEMENT_TIMEOUT_MS", "300000")
)
HTTP_EXPORT_STATEMENT_TIMEOUT_MS = int(
    os.environ.get("EXPORT_HTTP_STATEMENT_TIMEOUT_MS", "40000")
)


def export_connection_settings(statement_timeout_ms=None):
    """Connection arguments for Database B: integration_db.

    One definition, so a pooled connection and a one-off connection cannot drift
    apart on the settings that matter - read-only enforcement above all.
    """
    timeout_ms = int(
        DEFAULT_EXPORT_STATEMENT_TIMEOUT_MS
        if statement_timeout_ms is None
        else statement_timeout_ms
    )
    return dict(
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
        connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10")),
        options=(
            "-c default_transaction_read_only=on "
            f"-c statement_timeout={timeout_ms}"
        ),
        row_factory=dict_row,
    )


def get_export_connection(statement_timeout_ms=None):
    # Database B: integration_db. All export, LOS, cost-source, and Supplement
    # access is read-only at both the role and session level.
    return psycopg.connect(**export_connection_settings(statement_timeout_ms))


def export_conninfo(statement_timeout_ms=None):
    """The same settings as a conninfo string, for a pool to build from."""
    settings = export_connection_settings(statement_timeout_ms)
    # row_factory is a connection-object argument, not a libpq parameter; a pool
    # takes it separately.
    settings.pop("row_factory", None)
    return make_conninfo(**settings)


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
        connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10")),
    )
