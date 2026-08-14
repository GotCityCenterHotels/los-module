import unittest

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ImportJobDefinitionTests(unittest.TestCase):
    def test_migration_enforces_one_active_job_per_family(self):
        migration = (
            ROOT / "sql" / "migrations" / "005_import_jobs.sql"
        ).read_text(encoding="utf-8").lower()
        self.assertIn("create table if not exists functions.import_jobs", migration)
        self.assertIn("where status in ('queued', 'running', 'retrying')", migration)
        self.assertIn("create unique index", migration)

    def test_queue_configuration_serializes_imports_and_bounds_retries(self):
        host = (ROOT / "host.json").read_text(encoding="utf-8")
        self.assertIn('"batchSize": 1', host)
        self.assertIn('"newBatchThreshold": 0', host)
        self.assertIn('"maxDequeueCount": 3', host)

    def test_source_index_script_covers_every_asof_lookup(self):
        indexes = (
            ROOT / "sql" / "supplement_index" / "source_history_indexes.sql"
        ).read_text(encoding="utf-8").lower()
        for table in (
            "resource_history",
            "resource_category_assignment_history",
            "resource_category_history",
        ):
            self.assertIn(f"on {table}", indexes)
        self.assertGreaterEqual(indexes.count("snapshot_valid_from desc"), 3)


if __name__ == "__main__":
    unittest.main()
