import os

from psycopg_pool import ConnectionPool


connection_string = os.environ["POSTGRES_CONNECTION_STRING"]


pool = ConnectionPool(
    conninfo=connection_string,
    min_size=0,
    max_size=5,
    open=True,
)