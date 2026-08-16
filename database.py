import os

from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool


def _setting(primary, fallback, default=None):
    value = os.environ.get(primary, os.environ.get(fallback, default))
    if value is None:
        raise KeyError(primary)
    return value


# Database B: integration_db. This pool is intentionally read-only and is
# shared by the existing LOS read paths. Database A writes use cost_database.
DB_HOST = _setting("INTEGRATION_DB_HOST", "DB_HOST")
DB_PORT = _setting("INTEGRATION_DB_PORT", "DB_PORT", "5432")
DB_NAME = _setting("INTEGRATION_DB_NAME", "DB_NAME", "integration_db")
DB_USER = _setting("INTEGRATION_DB_USER", "DB_USER")
DB_PASSWORD = _setting("INTEGRATION_DB_PASSWORD", "DB_PASSWORD")
DB_SSLMODE = _setting("INTEGRATION_DB_SSLMODE", "DB_SSLMODE", "require")


connection_string = make_conninfo(
    host=DB_HOST,
    port=DB_PORT,
    dbname=DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD,
    sslmode=DB_SSLMODE,
    connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10")),
    application_name="los-functions-integration",
    options="-c default_transaction_read_only=on -c statement_timeout=300000",
)


# min_size defaults to 0 and is raised to 1 only by the deployed app settings
# (infra/configure-performance.ps1). A code default of 1 would make every unit
# test process open a background thread reconnecting to the placeholder
# localhost credentials the test modules set up, forever.
#
# Raising it is only half the fix, and the halves are worthless apart: a warm
# pool needs a live worker to hold it, so min_size without an always-ready
# instance buys nothing on a plan that scales to zero, and an always-ready
# instance without min_size gives a warm interpreter holding an empty pool -
# the first request still pays the full TLS+SCRAM handshake.
pool = ConnectionPool(
    conninfo=connection_string,
    min_size=int(os.environ.get("INTEGRATION_DB_POOL_MIN_SIZE", "0")),
    max_size=int(os.environ.get("INTEGRATION_DB_POOL_MAX_SIZE", "4")),
    timeout=float(os.environ.get("DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "10")),
    max_waiting=int(os.environ.get("DB_POOL_MAX_WAITING", "16")),
    max_idle=float(os.environ.get("DB_POOL_MAX_IDLE_SECONDS", "300")),
    max_lifetime=float(os.environ.get("DB_POOL_MAX_LIFETIME_SECONDS", "1800")),
    check=ConnectionPool.check_connection,
    open=True,
)
