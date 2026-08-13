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
from services import supplement_schema_service


class SupplementSourceSafetyTests(unittest.TestCase):
    def test_booking_source_is_direct_tenant_safe_and_bounded(self):
        query = " ".join(supplement_source.BOOKING_LIFECYCLE_SQL.lower().split())
        self.assertIn("from order_item_current", query)
        self.assertIn("join reservation_current", query)
        self.assertNotIn("service_order_note_current", query)
        self.assertNotIn("supplement_source_relation", query)
        self.assertIn("oi.start_utc >=", query)
        self.assertIn("oi.start_utc <", query)
        self.assertIn("rc.tenant_key = oi.tenant_key", query)
        self.assertIn("requested.id = rc.requested_resource_category_id", query)
        self.assertIn("left join resource_category_current requested", query)
        self.assertIn("coalesce(assigned.category_id, requested.id)", query)
        self.assertIn("oi.amount_gross_value", query)
        self.assertIn("rc.cancelled_utc::date", query)
        self.assertIn("oi.canceled_utc::date", query)

    def test_inventory_source_has_hybrid_boundary_and_both_denominators(self):
        query = " ".join(supplement_source.INVENTORY_SQL.lower().split())
        self.assertIn("resource_history", query)
        self.assertIn("resource_category_assignment_history", query)
        self.assertIn("date '2026-02-27'", query)
        self.assertIn("physical_inventory", query)
        self.assertIn("sellable_inventory", query)
        self.assertIn("r.state <> 'outoforder'", query)
        self.assertIn("'approximated-current'", query)

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

    def test_profile_gate_requires_stay_date_pruning_or_index_access(self):
        sequential = [{"Plan": {"Node Type": "Seq Scan", "Filter": "start_utc = $1"}}]
        indexed = [{"Plan": {
            "Node Type": "Index Scan",
            "Index Cond": "start_utc >= $1 AND start_utc < $2",
        }}]
        pruned = [{"Plan": {"Node Type": "Append", "Subplans Removed": 12}}]
        self.assertFalse(profile_supplement_source._uses_bounded_access(sequential))
        self.assertTrue(profile_supplement_source._uses_bounded_access(indexed))
        self.assertTrue(profile_supplement_source._uses_bounded_access(pruned))
        broad = [{"Plan": {
            "Node Type": "Seq Scan", "Relation Name": "order_item_current"
        }}]
        self.assertTrue(profile_supplement_source._has_broad_scan(
            broad, {"order_item_current"}
        ))

    def test_migration_contains_partitioned_snapshots_and_publication_pointer(self):
        migration = (
            Path(__file__).resolve().parent.parent
            / "sql" / "migrations" / "003_supplement_read_model.sql"
        ).read_text(encoding="utf-8").lower()
        self.assertGreaterEqual(migration.count("partition by range (snapshot_date)"), 3)
        self.assertIn("supplement_publication", migration)
        self.assertIn("ensure_supplement_month_partitions", migration)
        self.assertIn("space_room_category_id uuid", migration)
        self.assertIn("inventory_quality", migration)
        self.assertIn("primary key (hotel_code, room_category_id)", migration)

    def test_follow_up_migration_upgrades_already_applied_003_schema(self):
        migration = (
            Path(__file__).resolve().parent.parent
            / "sql" / "migrations" / "004_supplement_lifecycle_ids.sql"
        ).read_text(encoding="utf-8").lower()
        self.assertIn("delete from functions.supplement_publication", migration)
        self.assertIn("drop table if exists functions.supplement_snapshot_inventory", migration)
        self.assertIn("space_room_category_id uuid not null", migration)
        self.assertIn("inventory_quality text not null", migration)
        self.assertEqual(
            [name for name, _path in supplement_schema_service.MIGRATIONS],
            ["003_supplement_read_model", "004_supplement_lifecycle_ids"],
        )

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

    def test_database_a_materialization_uses_lifecycle_boundaries_and_one_room(self):
        service = (
            Path(__file__).resolve().parent.parent
            / "services" / "supplement_sync_service.py"
        ).read_text(encoding="utf-8")
        materialize = service.split("def _materialize_snapshot_facts", 1)[1].split(
            "def _validate_stages", 1
        )[0]
        normalized = " ".join(materialize.lower().split())
        self.assertIn("reservation_created_date <= s.snapshot_date", normalized)
        self.assertIn("reservation_cancelled_date > s.snapshot_date", normalized)
        self.assertIn("item_created_date <= s.snapshot_date", normalized)
        self.assertIn("item_cancelled_date > s.snapshot_date", normalized)
        self.assertIn("1::numeric as assigned_rooms", normalized)
        self.assertIn("sum(gross_revenue)::numeric as room_revenue", normalized)
        self.assertIn("reservation_id", normalized)
        validation = service.split("def _validate_stages", 1)[1].split(
            "def _ensure_partitions", 1
        )[0]
        self.assertIn("requested_category_id IS NULL", validation)
        self.assertIn("space_category_id IS NULL", validation)
        self.assertIn("ON CONFLICT (hotel_code, room_category_id)", service)
        self.assertIn("snapshot_date DESC", service)
        self.assertNotIn("ORDER BY enterprise_id, snapshot_date", service)
        self.assertIn(
            "ORDER BY source.enterprise_id, source.snapshot_date DESC",
            service,
        )


if __name__ == "__main__":
    unittest.main()
