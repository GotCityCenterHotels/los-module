import os
import unittest

from unittest.mock import patch

from shared import db


class SharedDatabaseConfigurationTests(unittest.TestCase):
    def test_export_connection_uses_database_b_settings(self):
        settings = {
            "POSTGRES_HOST": "shared-host",
            "POSTGRES_USER": "shared-user",
            "POSTGRES_PASSWORD": "shared-password",
            "EXPORT_POSTGRES_HOST": "database-b-host",
            "EXPORT_POSTGRES_DB": "database-b",
            "EXPORT_POSTGRES_USER": "database-b-user",
            "EXPORT_POSTGRES_PASSWORD": "database-b-password",
        }

        with patch.dict(os.environ, settings, clear=True), patch.object(
            db.psycopg,
            "connect",
        ) as connect:
            db.get_export_connection()

        connection_options = connect.call_args.kwargs
        self.assertEqual(connection_options["host"], "database-b-host")
        self.assertEqual(connection_options["dbname"], "database-b")
        self.assertEqual(connection_options["user"], "database-b-user")

    def test_import_connection_uses_database_a_cost_settings(self):
        settings = {
            "POSTGRES_HOST": "shared-host",
            "POSTGRES_DB": "default-import-database",
            "POSTGRES_USER": "shared-user",
            "POSTGRES_PASSWORD": "shared-password",
            "COST_DB_HOST": "database-a-host",
            "COST_DB_NAME": "database-a",
            "COST_DB_USER": "database-a-user",
            "COST_DB_PASSWORD": "database-a-password",
        }

        with patch.dict(os.environ, settings, clear=True), patch.object(
            db.psycopg,
            "connect",
        ) as connect:
            db.get_import_connection()

        connection_options = connect.call_args.kwargs
        self.assertEqual(connection_options["host"], "database-a-host")
        self.assertEqual(connection_options["dbname"], "database-a")
        self.assertEqual(connection_options["user"], "database-a-user")


if __name__ == "__main__":
    unittest.main()
