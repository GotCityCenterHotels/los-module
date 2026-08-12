from pathlib import Path

from .db import get_export_connection, get_import_connection


SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def read_sql(relative_path):
    sql_path = SQL_DIR / relative_path

    if not sql_path.exists():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")

    return sql_path.read_text(encoding="utf-8")


def fetch_export_rows(export_sql_file):
    sql = read_sql(export_sql_file)

    with get_export_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


def import_rows(import_sql_file, rows):
    if not rows:
        return 0

    sql = read_sql(import_sql_file)

    with get_import_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)

    return len(rows)

def transfer_dataset(export_sql_file, import_sql_file, batch_size=5000):
    export_sql = read_sql(export_sql_file)
    import_sql = read_sql(import_sql_file)

    total_exported = 0
    total_imported = 0

    with get_export_connection() as export_conn:
        with get_import_connection() as import_conn:
            with export_conn.cursor() as export_cur:
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