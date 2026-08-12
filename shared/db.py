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
    # Database B: operational/source data such as enterprise_current.
    return psycopg.connect(
        host=_setting("EXPORT_POSTGRES_HOST", "POSTGRES_HOST"),
        dbname=_setting("EXPORT_POSTGRES_DB"),
        user=_setting("EXPORT_POSTGRES_USER", "POSTGRES_USER"),
        password=_setting("EXPORT_POSTGRES_PASSWORD", "POSTGRES_PASSWORD"),
        port=int(
            _setting(
                "EXPORT_POSTGRES_PORT",
                "POSTGRES_PORT",
                default="5432",
            )
        ),
        sslmode=_setting(
            "EXPORT_POSTGRES_SSLMODE",
            "POSTGRES_SSLMODE",
            default="require",
        ),
        row_factory=dict_row,
    )


def get_import_connection():
    # Database A: the same cost/functions database used by cost_database.py.
    return psycopg.connect(
        host=_setting("COST_DB_HOST", "POSTGRES_HOST"),
        dbname=_setting("COST_DB_NAME", "POSTGRES_DB"),
        user=_setting("COST_DB_USER", "POSTGRES_USER"),
        password=_setting("COST_DB_PASSWORD", "POSTGRES_PASSWORD"),
        port=int(
            _setting("COST_DB_PORT", "POSTGRES_PORT", default="5432")
        ),
        sslmode=_setting(
            "COST_DB_SSLMODE",
            "POSTGRES_SSLMODE",
            default="require",
        ),
    )
