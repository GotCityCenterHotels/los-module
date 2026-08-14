import os

from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool


def _setting(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    if default is not None:
        return default
    raise KeyError(names[0])


# Database A is the writable PostgreSQL application database. It intentionally
# never falls back to DB_* because those settings identify integration_db.
_app_db_name = _setting("COST_DB_NAME", "POSTGRES_DB")
if _app_db_name.lower() == "integration_db":
    raise RuntimeError("Database A cannot be integration_db")

connection_string = make_conninfo(
    host=_setting("COST_DB_HOST", "POSTGRES_HOST"),
    port=_setting("COST_DB_PORT", "POSTGRES_PORT", default="5432"),
    dbname=_app_db_name,
    user=_setting("COST_DB_USER", "POSTGRES_USER"),
    password=_setting("COST_DB_PASSWORD", "POSTGRES_PASSWORD"),
    sslmode=_setting(
        "COST_DB_SSLMODE",
        "POSTGRES_SSLMODE",
        default="require",
    ),
    connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10")),
    application_name="los-functions-app",
    # Static Web Apps cuts off a linked-backend call at ~45s, so an HTTP query
    # that outlives that is pure waste: it keeps holding a pooled connection and
    # a Postgres backend long after the client gave up. Default tight and let
    # background jobs opt out via apply_background_timeouts().
    #
    # statement_timeout also bounds pg_advisory_lock() waits in the schema
    # services, which would otherwise block forever behind a leaked session lock.
    options=(
        f"-c statement_timeout={int(os.environ.get('DB_STATEMENT_TIMEOUT_MS', '40000'))}"
        f" -c lock_timeout={int(os.environ.get('DB_LOCK_TIMEOUT_MS', '5000'))}"
    ),
)


# Import/sync workers legitimately run for minutes under the 30 minute
# functionTimeout. They raise the ceiling per transaction rather than the pool
# defaulting loose, so a read path that forgets to opt in fails fast instead of
# hanging past the proxy timeout.
BACKGROUND_STATEMENT_TIMEOUT_MS = int(
    os.environ.get("DB_BACKGROUND_STATEMENT_TIMEOUT_MS", "1500000")
)


def apply_background_timeouts(cursor):
    """Lift the HTTP-oriented statement timeout for the current transaction."""
    # SET LOCAL takes no placeholders; the value is int-coerced above.
    cursor.execute(
        f"SET LOCAL statement_timeout = {BACKGROUND_STATEMENT_TIMEOUT_MS}"
    )


cost_pool = ConnectionPool(
    conninfo=connection_string,
    min_size=0,
    max_size=int(os.environ.get("APP_DB_POOL_MAX_SIZE", "4")),
    timeout=float(os.environ.get("DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "10")),
    max_waiting=int(os.environ.get("DB_POOL_MAX_WAITING", "16")),
    max_idle=float(os.environ.get("DB_POOL_MAX_IDLE_SECONDS", "300")),
    max_lifetime=float(os.environ.get("DB_POOL_MAX_LIFETIME_SECONDS", "1800")),
    check=ConnectionPool.check_connection,
    open=True,
)
