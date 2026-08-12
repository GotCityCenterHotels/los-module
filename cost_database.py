import os

from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool


def _setting(name, fallback_name=None, default=None):
    value = os.environ.get(name)
    if value is not None:
        return value
    if fallback_name is not None:
        value = os.environ.get(fallback_name)
        if value is not None:
            return value
    if default is not None:
        return default
    raise KeyError(name)


# Cost data is stored in a separate PostgreSQL database. Dedicated COST_DB_*
# settings allow it to live on another server, while the fallbacks keep local
# and same-server deployments concise.
connection_string = make_conninfo(
    host=_setting("COST_DB_HOST", "DB_HOST"),
    port=_setting("COST_DB_PORT", "DB_PORT", "5432"),
    dbname=_setting("COST_DB_NAME", default="postgres"),
    user=_setting("COST_DB_USER", "DB_USER"),
    password=_setting("COST_DB_PASSWORD", "DB_PASSWORD"),
    sslmode=_setting("COST_DB_SSLMODE", "DB_SSLMODE", "require"),
)


cost_pool = ConnectionPool(
    conninfo=connection_string,
    min_size=0,
    max_size=5,
    open=True,
)
