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
    options="-c default_transaction_read_only=on -c statement_timeout=300000",
)


pool = ConnectionPool(
    conninfo=connection_string,
    min_size=0,
    max_size=5,
    open=True,
)
