import os
import unittest

from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from services import cost_publication_service


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def __enter__(self): return self
    def __exit__(self, *args): return False

    def execute(self, query, parameters=None):
        self.executed.append((query, parameters))

    def fetchone(self):
        return self.rows.pop(0)


class Connection:
    def __init__(self, cursor): self.cursor_instance = cursor
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self): return self.cursor_instance


class Pool:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.connection_count = 0

    def connection(self):
        self.connection_count += 1
        return Connection(self.cursor_instance)


class CostPublicationTests(unittest.TestCase):
    def setUp(self):
        cost_publication_service._reset_publication_cache()

    def tearDown(self):
        cost_publication_service._reset_publication_cache()

    def test_publication_lookup_is_reused_inside_the_short_window(self):
        cursor = Cursor([(11,)])
        pool = Pool(cursor)
        with patch.object(
            cost_publication_service,
            "ensure_cost_settings_schema",
        ), patch.object(cost_publication_service, "cost_pool", pool):
            first = cost_publication_service.fetch_cost_publication_version()
            second = cost_publication_service.fetch_cost_publication_version()

        self.assertEqual((first, second), (11, 11))
        self.assertEqual(pool.connection_count, 1)

    def test_owned_advance_updates_the_worker_cache_after_commit(self):
        cursor = Cursor([(12,)])
        pool = Pool(cursor)
        with patch.object(
            cost_publication_service,
            "ensure_cost_settings_schema",
        ), patch.object(cost_publication_service, "cost_pool", pool):
            version = cost_publication_service.advance_cost_publication(
                "import:parking"
            )
            cached = cost_publication_service.fetch_cost_publication_version()

        self.assertEqual((version, cached), (12, 12))
        self.assertEqual(pool.connection_count, 1)
        self.assertIn("version = functions.cost_publication.version + 1", cursor.executed[0][0])
        self.assertEqual(cursor.executed[0][1], ("import:parking",))

    def test_migration_creates_only_a_database_a_publication_pointer(self):
        migration = (
            Path(__file__).resolve().parent.parent
            / "sql" / "migrations" / "017_cost_publication.sql"
        ).read_text(encoding="utf-8").lower()
        self.assertIn("create table if not exists functions.cost_publication", migration)
        self.assertIn("primary key", migration)
        self.assertNotIn("integration_db", migration)


if __name__ == "__main__":
    unittest.main()
