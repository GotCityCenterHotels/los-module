from pathlib import Path

from .db import get_export_connection, get_import_connection


SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def read_sql(relative_path):
    sql_path = SQL_DIR / relative_path

    if not sql_path.exists():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")

    return sql_path.read_text(encoding="utf-8")


def transfer_dataset(export_sql_file, import_sql_file, batch_size=5000):
    export_sql = read_sql(export_sql_file)
    import_sql = read_sql(import_sql_file)

    total_exported = 0
    total_imported = 0

    with get_export_connection() as export_conn:
        with get_import_connection() as import_conn:
            # A named cursor keeps the result set server-side in integration_db
            # and streams it in batches. An unnamed psycopg3 cursor buffers the
            # ENTIRE result into worker memory on execute(), so fetchmany() was
            # only slicing an already-materialised list and batch_size bounded
            # nothing. DECLARE CURSOR is a read operation - it does not write to
            # integration_db, which stays read-only.
            cursor_name = f"export_{Path(export_sql_file).stem}"
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

    return {
        "export_rows": total_exported,
        "import_rows": total_imported,
    }