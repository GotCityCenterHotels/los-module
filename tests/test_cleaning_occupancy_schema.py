import os
import re
import unittest

from pathlib import Path


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO_ROOT / "sql" / "migrations"
BASE_SCHEMA = REPO_ROOT / "sql" / "tables" / "cost_input_settings.sql"


def read(path):
    return path.read_text(encoding="utf-8")


class CleaningUniquenessTests(unittest.TestCase):
    """Cleaning rows are one per (room category, occupancy).

    Uniqueness keyed on category_name alone rejects every row after the first,
    which surfaced as a 500 on save. The old key existed in two forms - a
    standalone UNIQUE INDEX from migration 001 and a table-level UNIQUE
    constraint in the base schema - and migration 012 only dropped the
    constraint, so the index kept enforcing the old rule.
    """

    def test_migration_001_created_the_index_form(self):
        # Guards the premise: if 001 ever stops creating this, 013's DROP INDEX
        # becomes dead code and this test should be revisited.
        text = read(MIGRATIONS / "001_cost_settings_enterprise_text.sql")
        self.assertIn("ux_cost_cleaning_categories_enterprise_name", text)

    def test_the_standalone_unique_index_is_dropped(self):
        text = read(MIGRATIONS / "013_cleaning_occupancy_unique_fix.sql")
        self.assertRegex(
            text,
            r"DROP INDEX IF EXISTS\s+functions\.ux_cost_cleaning_categories_enterprise_name",
        )

    def test_the_table_level_unique_constraint_is_dropped(self):
        text = read(MIGRATIONS / "013_cleaning_occupancy_unique_fix.sql")
        self.assertIn(
            "cost_cleaning_categories_enterprise_id_category_name_key", text
        )

    def test_the_occupancy_aware_key_is_created(self):
        text = read(MIGRATIONS / "013_cleaning_occupancy_unique_fix.sql")
        self.assertRegex(
            text,
            r"CREATE UNIQUE INDEX[^;]*ux_cost_cleaning_categories_occupancy[^;]*"
            r"\(\s*enterprise_id,\s*category_name,\s*occupancy\s*\)",
        )

    def test_base_schema_keys_cleaning_on_occupancy_too(self):
        # A fresh install must not recreate the bug that 013 exists to fix.
        text = read(BASE_SCHEMA)
        self.assertIn("UNIQUE (enterprise_id, category_name, occupancy)", text)
        self.assertNotIn("UNIQUE (enterprise_id, category_name),", text)

    def test_base_schema_defines_the_occupancy_column(self):
        text = read(BASE_SCHEMA)
        self.assertRegex(text, r"occupancy\s+integer\s+NOT NULL")
        self.assertIn("resource_category_id", text)

    def test_every_cleaning_migration_is_registered_in_order(self):
        from services import cost_schema_service

        registered = [name for name, _ in cost_schema_service.MIGRATIONS]
        self.assertIn("012_cleaning_occupancy", registered)
        self.assertIn("013_cleaning_occupancy_unique_fix", registered)
        # 013 repairs what 012 did; applying it first would leave the old key.
        self.assertLess(
            registered.index("012_cleaning_occupancy"),
            registered.index("013_cleaning_occupancy_unique_fix"),
        )

    def test_registered_migration_files_all_exist(self):
        from services import cost_schema_service

        for name, path in cost_schema_service.MIGRATIONS:
            with self.subTest(migration=name):
                self.assertTrue(path.exists(), f"{name} is registered but missing")

    def test_registered_migrations_self_record_under_their_own_name(self):
        # A migration that records the wrong name re-runs forever.
        from services import cost_schema_service

        for name, path in cost_schema_service.MIGRATIONS:
            with self.subTest(migration=name):
                text = read(path)
                self.assertRegex(
                    text,
                    re.escape(f"VALUES ('{name}')"),
                    f"{name} does not insert its own name into schema_migrations",
                )


if __name__ == "__main__":
    unittest.main()
