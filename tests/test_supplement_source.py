import os
import unittest

from pathlib import Path
from unittest.mock import MagicMock, patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "integration_db")
os.environ.setdefault("DB_USER", "readonly")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from queries import supplement_source
import profile_supplement_source
from shared import db


class SupplementSourceSafetyTests(unittest.TestCase):
    def test_source_relation_is_required_and_schema_qualified(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SUPPLEMENT_SOURCE_RELATION"):
                supplement_source._source_relation()

        with patch.dict(
            os.environ,
            {"SUPPLEMENT_SOURCE_RELATION": "unsafe; delete from x"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "schema-qualified"):
                supplement_source._source_relation()

    def test_source_projection_is_select_only_and_bounded(self):
        with patch.dict(
            os.environ,
            {"SUPPLEMENT_SOURCE_RELATION": "reporting.safe_projection"},
            clear=True,
        ):
            query = supplement_source._snapshot_select().as_string()
        normalized = " ".join(query.lower().split())
        self.assertTrue(normalized.startswith("select "))
        self.assertIn("view_date >=", normalized)
        self.assertIn("stay_date >=", normalized)
        self.assertNotIn(" insert ", normalized)
        self.assertNotIn(" update ", normalized)
        self.assertNotIn(" delete ", normalized)

    def test_database_a_rejects_integration_db_name(self):
        settings = {
            "POSTGRES_HOST": "localhost",
            "POSTGRES_DB": "integration_db",
            "POSTGRES_USER": "writer",
            "POSTGRES_PASSWORD": "not-used",
        }
        with patch.dict(os.environ, settings, clear=True):
            with self.assertRaisesRegex(RuntimeError, "cannot be integration_db"):
                db.get_import_connection()

    def test_supplement_source_requires_dedicated_integration_settings(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "dedicated Database B settings"):
                supplement_source._require_integration_settings()

    def test_source_connection_verifies_database_and_read_only_transaction(self):
        settings = {
            "INTEGRATION_DB_HOST": "source-host",
            "INTEGRATION_DB_NAME": "integration_db",
            "INTEGRATION_DB_USER": "reader",
            "INTEGRATION_DB_PASSWORD": "secret",
        }
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = {
            "database_name": "integration_db",
            "read_only": "on",
        }
        connection.cursor.return_value = cursor
        with patch.dict(os.environ, settings, clear=True), patch.object(
            supplement_source,
            "get_export_connection",
            return_value=connection,
        ):
            with supplement_source._read_only_source_connection() as yielded:
                self.assertIs(yielded, connection)
        cursor.execute.assert_called_once()
        settings = {
            "INTEGRATION_DB_HOST": "source-host",
            "INTEGRATION_DB_NAME": "another-database",
            "INTEGRATION_DB_USER": "reader",
            "INTEGRATION_DB_PASSWORD": "secret",
        }
        with patch.dict(os.environ, settings, clear=True):
            with self.assertRaisesRegex(RuntimeError, "must be integration_db"):
                supplement_source._require_integration_settings()

    def test_profile_gate_requires_view_date_pruning_or_index_access(self):
        sequential = [{"Plan": {"Node Type": "Seq Scan", "Filter": "view_date = $1"}}]
        indexed = [{"Plan": {
            "Node Type": "Index Scan",
            "Index Cond": "view_date >= $1 AND view_date < $2",
        }}]
        pruned = [{"Plan": {"Node Type": "Append", "Subplans Removed": 12}}]
        self.assertFalse(profile_supplement_source._uses_bounded_access(sequential))
        self.assertTrue(profile_supplement_source._uses_bounded_access(indexed))
        self.assertTrue(profile_supplement_source._uses_bounded_access(pruned))

    def test_migration_contains_partitioned_snapshots_and_publication_pointer(self):
        migration = (
            Path(__file__).resolve().parent.parent
            / "sql" / "migrations" / "003_supplement_read_model.sql"
        ).read_text(encoding="utf-8").lower()
        self.assertGreaterEqual(migration.count("partition by range (snapshot_date)"), 3)
        self.assertIn("supplement_publication", migration)
        self.assertIn("ensure_supplement_month_partitions", migration)

    def test_sync_keeps_final_facts_and_replaces_only_discovered_snapshots(self):
        service = (
            Path(__file__).resolve().parent.parent
            / "services" / "supplement_sync_service.py"
        ).read_text(encoding="utf-8")
        retention = service.split("def _apply_retention", 1)[1].split(
            "def sync_supplement", 1
        )[0]
        self.assertNotIn("DELETE FROM functions.supplement_latest", retention)
        self.assertIn("snapshot_date = ANY(%(snapshot_dates)s)", service)


if __name__ == "__main__":
    unittest.main()
