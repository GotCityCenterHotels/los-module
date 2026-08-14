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
