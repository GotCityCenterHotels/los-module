import unittest

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class UnifiedHotelMigrationTests(unittest.TestCase):
    def test_online_migration_is_additive_and_validates_fact_coverage(self):
        migration = (ROOT / "sql" / "migrations" / "006_unified_hotels.sql").read_text(
            encoding="utf-8"
        ).lower()
        self.assertIn("create table if not exists functions.hotels", migration)
        self.assertIn("rows in functions.% have no hotel dimension row", migration)
        self.assertIn("alter column hotel_name drop not null", migration)
        self.assertNotIn("drop table if exists functions.cost_properties", migration)
        self.assertNotIn("drop table if exists functions.supplement_hotels", migration)

    def test_cleanup_is_explicit_and_not_automatic(self):
        cleanup = (
            ROOT / "sql" / "migrations" / "007_remove_legacy_hotel_tables.sql"
        ).read_text(encoding="utf-8").lower()
        self.assertIn("functions.cost_properties: % hotels were not migrated", cleanup)
        self.assertIn("functions.supplement_hotels: % hotels were not migrated", cleanup)
        self.assertIn("drop column if exists hotel_name", cleanup)
        self.assertIn("drop table if exists functions.cost_properties", cleanup)
        self.assertIn("drop table if exists functions.supplement_hotels", cleanup)

        for service_path in (
            ROOT / "services" / "cost_schema_service.py",
            ROOT / "services" / "supplement_schema_service.py",
        ):
            self.assertNotIn(
                "007_remove_legacy_hotel_tables",
                service_path.read_text(encoding="utf-8"),
            )

    def test_cost_and_supplement_migrations_share_one_database_lock(self):
        services = [
            (ROOT / "services" / name).read_text(encoding="utf-8")
            for name in ("cost_schema_service.py", "supplement_schema_service.py")
        ]
        for service in services:
            self.assertIn('("functions.application_schema",)', service)


if __name__ == "__main__":
    unittest.main()
