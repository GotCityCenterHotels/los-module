"""The shared fast path for schema bootstrap.

Every schema service runs `ensure_*_schema()` on each worker process's first
request, and each one used to take a cluster-wide session advisory lock and then
issue one SELECT per migration to discover that there was nothing to do. On a
cold `/api/los/facts` that was about eleven round trips before the first byte of
real work - and worse than the round trips, the lock is shared, so the
`/api/los/hotels` request the same page fires in parallel blocked behind it
rather than running alongside.

The check itself does not need the lock. A migration name is recorded in the same
transaction that applies it, so a worker that can see every expected name knows
the schema is current without coordinating with anyone. Only a worker that finds
something missing has to take the lock and do the work.

cost_schema_service had this fast path already; this is that logic, extracted so
the other three share one implementation rather than three near-copies.
"""


def applied_migrations(cursor, names):
    """Which of ``names`` are recorded, in one round trip.

    Returns None when the bookkeeping table itself does not exist, which means
    nothing has ever been applied and the full bootstrap has to run.
    """
    cursor.execute("SELECT to_regclass('functions.schema_migrations')")
    if cursor.fetchone()[0] is None:
        return None
    cursor.execute(
        "SELECT migration_name FROM functions.schema_migrations "
        "WHERE migration_name = ANY(%s)",
        (list(names),),
    )
    return {row[0] for row in cursor.fetchall()}


def pending_migrations(cursor, names):
    """The subset of ``names`` still to apply, in declared order.

    None means the bookkeeping table is missing, which is not the same answer as
    the empty list: one says "everything, and build the table first", the other
    says "nothing".
    """
    applied = applied_migrations(cursor, names)
    if applied is None:
        return None
    return [name for name in names if name not in applied]


def migrations_are_current(cursor, names):
    """True when every named migration is already recorded.

    Two round trips, no advisory lock. This is the only question the common case
    needs answered.
    """
    return pending_migrations(cursor, names) == []
