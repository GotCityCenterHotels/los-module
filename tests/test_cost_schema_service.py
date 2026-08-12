import os
import unittest


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")

from services import cost_schema_service


class FakeCursor:
    def __init__(self):
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
        if "schema_migrations WHERE" in self.last_query:
            return None
        if "to_regclass" in self.last_query:
            return (None,)
        return (True,)


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.rollback_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        self.rollback_count += 1


class FakePool:
    def __init__(self):
        self.connection_instance = FakeConnection()

    def connection(self):
        return self.connection_instance


class CostSchemaServiceTests(unittest.TestCase):
    def test_fresh_database_applies_base_schema_once(self):
        original_pool = cost_schema_service.cost_pool
        fake_pool = FakePool()
        cost_schema_service.cost_pool = fake_pool
        cost_schema_service._schema_ready = False

        try:
            cost_schema_service.ensure_cost_settings_schema()
            first_execution_count = len(fake_pool.connection_instance.cursor_instance.executions)
            cost_schema_service.ensure_cost_settings_schema()
        finally:
            cost_schema_service.cost_pool = original_pool
            cost_schema_service._schema_ready = False

        queries = [query for query, _ in fake_pool.connection_instance.cursor_instance.executions]
        self.assertTrue(any(
            "CREATE TABLE IF NOT EXISTS functions.cost_property_settings" in query
            for query in queries
        ))
        self.assertTrue(any("pg_advisory_unlock" in query for query in queries))
        self.assertEqual(
            len(fake_pool.connection_instance.cursor_instance.executions),
            first_execution_count,
        )

    def test_uuid_columns_are_converted_before_text_property_mapping(self):
        migration_sql = cost_schema_service.MIGRATION_PATH.read_text(encoding="utf-8")

        conversion_position = migration_sql.index(
            "ALTER COLUMN enterprise_id TYPE text"
        )
        mapping_position = migration_sql.index(
            "SET enterprise_id = property_map.enterprise_id"
        )
        self.assertLess(conversion_position, mapping_position)


if __name__ == "__main__":
    unittest.main()
