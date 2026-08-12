import os

import psycopg
from psycopg.rows import dict_row


def get_export_connection():
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        dbname=os.environ["EXPORT_POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        sslmode=os.environ.get("POSTGRES_SSLMODE", "require"),
        row_factory=dict_row,
    )


def get_import_connection():
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        sslmode=os.environ.get("POSTGRES_SSLMODE", "require"),
    )