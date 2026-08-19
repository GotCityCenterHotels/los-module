"""The schema bootstrap fast path.

Every schema service runs on each worker process's first request. All four used to
take a cluster-wide session advisory lock and then issue one SELECT per migration
to discover there was nothing to do - about eleven round trips on a cold
/api/los/facts. Worse than the round trips: the lock is shared, so the
/api/los/hotels request the same page fires in parallel blocked behind it instead
of running alongside it.

The property that matters is therefore not "fewer statements" but "no lock". If
the fast path ever starts taking one, the serialization comes back and nothing
else in the suite would notice.
"""

import os
import unittest


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from cost_database import cost_pool as real_cost_pool
from database import pool as real_export_pool
from services import (
    import_job_schema_service,
    los_schema_service,
    schema_bootstrap,
    supplement_schema_service,
)


real_cost_pool.close()
real_export_pool.close()


class RecordingCursor:
    """A cursor that answers as a fully-migrated database would."""

    def __init__(self, applied):
        self.applied = applied
        self.executions = []
        self.last_query = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters=None):
        self.last_query = query
        self.executions.append((query, parameters))

    def fetchone(self):
        if "to_regclass" in self.last_query:
            # The bookkeeping table exists.
            return ("functions.schema_migrations",)
        return (True,)

    def fetchall(self):
        return [(name,) for name in self.applied]


class RecordingConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, *args, **kwargs):
        return self.cursor_instance

    def rollback(self):
        pass


class RecordingPool:
    def __init__(self, applied):
        self.cursor_instance = RecordingCursor(applied)

    def connection(self):
        return RecordingConnection(self.cursor_instance)


class HelperTests(unittest.TestCase):
    def test_a_missing_bookkeeping_table_is_not_an_empty_pending_list(self):
        """None and [] are different answers.

        None means "everything, and build the table first"; [] means "nothing".
        Collapsing them would let a fresh database report itself current.
        """

        class NoTable(RecordingCursor):
            def fetchone(self):
                return (None,)

        cursor = NoTable([])
        self.assertIsNone(schema_bootstrap.pending_migrations(cursor, ["a"]))
        self.assertFalse(schema_bootstrap.migrations_are_current(cursor, ["a"]))

    def test_pending_migrations_keeps_declared_order(self):
        cursor = RecordingCursor(["b"])
        self.assertEqual(
            schema_bootstrap.pending_migrations(cursor, ["a", "b", "c"]),
            ["a", "c"],
        )

    def test_everything_recorded_reads_as_current(self):
        cursor = RecordingCursor(["a", "b"])
        self.assertTrue(schema_bootstrap.migrations_are_current(cursor, ["a", "b"]))

    def test_one_missing_name_is_not_current(self):
        cursor = RecordingCursor(["a"])
        self.assertFalse(schema_bootstrap.migrations_are_current(cursor, ["a", "b"]))

    def test_the_check_costs_two_round_trips(self):
        cursor = RecordingCursor(["a"])
        schema_bootstrap.migrations_are_current(cursor, ["a"])
        self.assertEqual(len(cursor.executions), 2)


class FastPathTests(unittest.TestCase):
    """Each service, against a database that is already fully migrated."""

    SERVICES = (
        (los_schema_service, "ensure_los_schema"),
        (import_job_schema_service, "ensure_import_job_schema"),
        (supplement_schema_service, "ensure_supplement_schema"),
    )

    def applied_names(self, service):
        if hasattr(service, "MIGRATIONS"):
            return [name for name, _ in service.MIGRATIONS]
        return [service.MIGRATION_NAME]

    def run_fast_path(self, service, entry_point):
        pool = RecordingPool(self.applied_names(service))
        original = service.cost_pool
        service.cost_pool = pool
        service._schema_ready = False
        try:
            getattr(service, entry_point)()
        finally:
            service.cost_pool = original
            service._schema_ready = False
        return [query for query, _ in pool.cursor_instance.executions]

    def test_a_current_schema_takes_no_advisory_lock(self):
        for service, entry_point in self.SERVICES:
            with self.subTest(service=service.__name__):
                queries = self.run_fast_path(service, entry_point)
                for query in queries:
                    self.assertNotIn("pg_advisory_lock", query)
                    self.assertNotIn("pg_advisory_unlock", query)

    def test_a_current_schema_issues_no_ddl(self):
        for service, entry_point in self.SERVICES:
            with self.subTest(service=service.__name__):
                queries = self.run_fast_path(service, entry_point)
                for query in queries:
                    self.assertNotIn("CREATE SCHEMA", query)
                    self.assertNotIn("CREATE TABLE", query)

    def test_the_fast_path_costs_two_round_trips(self):
        for service, entry_point in self.SERVICES:
            with self.subTest(service=service.__name__):
                queries = self.run_fast_path(service, entry_point)
                self.assertEqual(
                    len(queries),
                    2,
                    f"{service.__name__} used {len(queries)} statements: {queries}",
                )

    def test_the_result_is_remembered_so_later_requests_are_free(self):
        for service, entry_point in self.SERVICES:
            with self.subTest(service=service.__name__):
                pool = RecordingPool(self.applied_names(service))
                original = service.cost_pool
                service.cost_pool = pool
                service._schema_ready = False
                try:
                    getattr(service, entry_point)()
                    after_first = len(pool.cursor_instance.executions)
                    getattr(service, entry_point)()
                    after_second = len(pool.cursor_instance.executions)
                finally:
                    service.cost_pool = original
                    service._schema_ready = False
                self.assertEqual(after_first, after_second)


if __name__ == "__main__":
    unittest.main()
