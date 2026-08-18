"""Publication identity for the complete Cost Data response.

The source mirror remains read-only. This pointer lives in Database A beside
the imported facts and saved rulebook whose combined state it names.
"""

import os

from threading import Lock
from time import monotonic

from cost_database import cost_pool
from services.cost_schema_service import ensure_cost_settings_schema


_PUBLICATION_CACHE_SECONDS = float(
    os.environ.get("COST_PUBLICATION_CACHE_SECONDS", "5")
)
_publication_cache = None
_publication_lock = Lock()


def _version_from_row(row):
    if isinstance(row, dict):
        return int(row["version"])
    return int(row[0])


def remember_cost_publication(version):
    """Publish a committed version to this worker's short local cache."""
    global _publication_cache
    with _publication_lock:
        _publication_cache = (
            monotonic() + _PUBLICATION_CACHE_SECONDS,
            int(version),
        )


def _reset_publication_cache():
    """Test seam and explicit invalidation for a worker-local cache."""
    global _publication_cache
    with _publication_lock:
        _publication_cache = None


def fetch_cost_publication_version():
    """Return the current Database A publication version.

    Five seconds of worker-local reuse absorbs bursts without making this
    pointer another pair of PostgreSQL round trips on every request. It is well
    inside the 60-second browser freshness window already advertised by the
    Cost Data route.
    """
    with _publication_lock:
        cached = _publication_cache
        if cached is not None and monotonic() < cached[0]:
            return cached[1]

    ensure_cost_settings_schema()
    with cost_pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT version FROM functions.cost_publication WHERE singleton"
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("Cost Data publication row is missing")

    version = _version_from_row(row)
    remember_cost_publication(version)
    return version


def advance_cost_publication(reason, cursor=None):
    """Advance the pointer, optionally inside the caller's transaction.

    When a cursor is supplied the caller must call ``remember_cost_publication``
    only after its transaction commits. With an owned connection the commit
    happens here and the worker cache is updated immediately afterwards.
    """
    statement = """
        INSERT INTO functions.cost_publication (
            singleton, version, changed_at, reason
        )
        VALUES (true, 1, now(), %s)
        ON CONFLICT (singleton) DO UPDATE SET
            version = functions.cost_publication.version + 1,
            changed_at = now(),
            reason = EXCLUDED.reason
        RETURNING version
    """

    if cursor is not None:
        cursor.execute(statement, (reason,))
        return _version_from_row(cursor.fetchone())

    ensure_cost_settings_schema()
    with cost_pool.connection() as connection:
        with connection.cursor() as owned_cursor:
            owned_cursor.execute(statement, (reason,))
            version = _version_from_row(owned_cursor.fetchone())
    remember_cost_publication(version)
    return version
