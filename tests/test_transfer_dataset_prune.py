"""The stale-row prune must never run against an empty export.

The prune deletes every row in a 730-day window whose last_seen_at predates this
run. That is correct when the export has just refreshed the window, and
destructive when the export returned nothing: the first fetchmany comes back
empty, the loop breaks, and control falls straight through to the prune, which
then empties the window instead of refreshing it. Because pruned_rows > 0
advances the cost publication, that emptiness was published as a fresh reading.

Zero exported rows from a full-window rebuild is not a real state. It means a
hard-coded predicate stopped matching - service.name = 'Stay',
enterprise.tenant_key = 'GCCH', the 'SpaceOrder' item type - while the builder
still resolved its columns.
"""

import os
import unittest

from unittest.mock import patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("COST_DB_HOST", "localhost")
os.environ.setdefault("COST_DB_NAME", "app-test")
os.environ.setdefault("COST_DB_USER", "app-test")
os.environ.setdefault("COST_DB_PASSWORD", "not-used")

from shared import sql_runner


class Cursor:
    """One cursor, recording what it was asked to run."""

    def __init__(self, owner, rows=None, name=None):
        self.owner = owner
        self.name = name
        self._batches = list(rows or [])
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, parameters=None):
        self.owner.executed.append((str(sql), parameters))
        if "DELETE FROM" in str(sql):
            self.rowcount = 4321

    def executemany(self, sql, parameters=None):
        self.owner.executed.append((str(sql), "many"))

    def fetchone(self):
        return ("2026-08-20T00:00:00+00:00",)

    def fetchmany(self, size):
        return self._batches.pop(0) if self._batches else []


class Connection:
    def __init__(self, batches=None):
        self.executed = []
        self.batches = batches or []
        self.committed = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, name=None, **kwargs):
        return Cursor(self, self.batches if name else None, name)

    def commit(self):
        self.committed += 1


class PruneGuardTests(unittest.TestCase):
    def _run(self, batches):
        export = Connection(batches)
        app_db = Connection()
        plan = {
            "export_sql": "SELECT 1",
            "prune_sql": "DELETE FROM functions.distribution_mix_data WHERE x",
        }
        with patch.object(
            sql_runner, "get_export_connection", return_value=export
        ), patch.object(
            sql_runner, "get_import_connection", return_value=app_db
        ), patch.object(
            sql_runner, "read_sql", return_value="INSERT INTO t VALUES (%s)"
        ):
            result = sql_runner.transfer_dataset(
                export_sql_builder=lambda cursor: plan,
                import_sql_file="import/upsert_distribution_mix_data.sql",
                name="distribution_mix",
            )
        deletes = [sql for sql, _ in app_db.executed if "DELETE FROM" in sql]
        return result, deletes

    def test_an_empty_export_does_not_prune(self):
        result, deletes = self._run(batches=[])

        self.assertEqual(deletes, [], "the prune ran against an empty export")
        self.assertEqual(result["pruned_rows"], 0)
        self.assertEqual(result["import_rows"], 0)
        # And it says so, so the run stops counting as a plain success. Without
        # this the pipeline records "success", the queue worker completes the
        # job, and the publication never moves - so no cache anywhere even
        # rebuilds.
        self.assertIn("skipped", result)
        self.assertIn("prune", result["skipped"].lower())

    def test_a_populated_export_still_prunes(self):
        result, deletes = self._run(batches=[[("a",), ("b",)]])

        self.assertEqual(len(deletes), 1, "the prune must still run normally")
        self.assertEqual(result["pruned_rows"], 4321)
        self.assertEqual(result["import_rows"], 2)
        self.assertNotIn("skipped", result)


if __name__ == "__main__":
    unittest.main()
