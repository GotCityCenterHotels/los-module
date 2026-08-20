import logging

from pathlib import Path

from .db import get_export_connection, get_import_connection


SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def read_sql(relative_path):
    sql_path = SQL_DIR / relative_path

    if not sql_path.exists():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")

    return sql_path.read_text(encoding="utf-8")


def transfer_dataset(
    export_sql_file=None,
    import_sql_file=None,
    batch_size=5000,
    export_sql_builder=None,
    name=None,
):
    """Stream one dataset from integration_db into the application database.

    Most datasets are a static file. `export_sql_builder` is for the two
    reservation-level mixes, whose source columns are not knowable from this
    repository: it is handed a cursor on the export connection and returns
    {"export_sql", "prune_sql"}, or None when this mirror cannot answer the
    question - in which case the dataset is skipped rather than failing the whole
    import and taking the working datasets down with it.
    """
    import_sql = read_sql(import_sql_file)
    prune_sql = None

    total_exported = 0
    total_imported = 0
    total_pruned = 0
    prune_refused = None

    with get_export_connection() as export_conn:
        if export_sql_builder is not None:
            # Resolved on the export connection before the streaming cursor is
            # declared: the builder reads information_schema to find out what
            # this mirror calls the columns it needs.
            with export_conn.cursor() as probe:
                plan = export_sql_builder(probe)
            if plan is None:
                return {
                    "export_rows": 0,
                    "import_rows": 0,
                    "pruned_rows": 0,
                    "skipped": (
                        "The source mirror does not carry the columns this "
                        "dataset needs; see the warning logged by the builder."
                    ),
                }
            export_sql = plan["export_sql"]
            prune_sql = plan.get("prune_sql")
        else:
            export_sql = read_sql(export_sql_file)

        with get_import_connection() as import_conn:
            # Read before anything is written, and committed, so it is strictly
            # earlier than the now() every upsert below stamps onto last_seen_at.
            # clock_timestamp() rather than now(), which is the transaction's own
            # start and could tie with the first batch's.
            with import_conn.cursor() as stamp:
                stamp.execute("SELECT clock_timestamp()")
                started_at = stamp.fetchone()[0]
            import_conn.commit()

            # A named cursor keeps the result set server-side in integration_db
            # and streams it in batches. An unnamed psycopg3 cursor buffers the
            # ENTIRE result into worker memory on execute(), so fetchmany() was
            # only slicing an already-materialised list and batch_size bounded
            # nothing. DECLARE CURSOR is a read operation - it does not write to
            # integration_db, which stays read-only.
            cursor_name = f"export_{name or Path(export_sql_file).stem}"
            with export_conn.cursor(name=cursor_name) as export_cur:
                export_cur.itersize = batch_size
                with import_conn.cursor() as import_cur:
                    export_cur.execute(export_sql)

                    while True:
                        rows = export_cur.fetchmany(batch_size)

                        if not rows:
                            break

                        import_cur.executemany(import_sql, rows)
                        import_conn.commit()

                        total_exported += len(rows)
                        total_imported += len(rows)

            # Only the mixes prune. Their rows are keyed by dimension, so a
            # combination that stops occurring has no row for the upsert to
            # overwrite and would keep its old figure for good.
            #
            # But never prune against nothing. The prune deletes every row in a
            # 730-day window whose last_seen_at predates this run, so an export
            # that returned zero rows - the first fetchmany comes back empty, the
            # loop breaks, and control falls straight through to here - deletes
            # the entire window instead of refreshing it. And because
            # pruned_rows > 0 advances the cost publication, that emptiness is
            # then published as if it were a fresh reading.
            #
            # Zero exported rows from a full-window rebuild is not a real state:
            # it means a hard-coded predicate stopped matching (service.name =
            # 'Stay', enterprise.tenant_key = 'GCCH', the 'SpaceOrder' item type)
            # while the builder still resolved its columns. The right answer is to
            # keep last night's data and say so, which is what
            # supplement_sync_service._inventory_variance_exceeds already does for
            # its own inventory.
            if prune_sql is not None and total_imported == 0:
                logging.warning(
                    "Refusing to prune %s: the export returned no rows, so the "
                    "prune would delete the whole window rather than refresh it",
                    name or import_sql_file,
                )
                prune_refused = (
                    "The export returned no rows, so the stale-row prune was "
                    "refused rather than emptying the window. Last night's "
                    "figures stand; the source predicates need checking."
                )
            elif prune_sql is not None:
                with import_conn.cursor() as prune_cur:
                    prune_cur.execute(prune_sql, {"started_at": started_at})
                    total_pruned = max(prune_cur.rowcount, 0)
                import_conn.commit()

    result = {
        "export_rows": total_exported,
        "import_rows": total_imported,
        "pruned_rows": total_pruned,
    }
    if prune_refused is not None:
        result["skipped"] = prune_refused
    return result
