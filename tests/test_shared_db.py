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
        self.assertIn("default_transaction_read_only=on", connection_options["options"])

    def test_export_connection_prefers_integration_db_settings(self):
        settings = {
            "DB_HOST": "legacy-host",
            "DB_USER": "legacy-user",
            "DB_PASSWORD": "legacy-password",
            "INTEGRATION_DB_HOST": "integration-host",
            "INTEGRATION_DB_NAME": "integration_db",
            "INTEGRATION_DB_USER": "readonly-user",
            "INTEGRATION_DB_PASSWORD": "readonly-password",
        }

        with patch.dict(os.environ, settings, clear=True), patch.object(
            db.psycopg,
            "connect",
        ) as connect:
            db.get_export_connection()

        options = connect.call_args.kwargs
        self.assertEqual(options["host"], "integration-host")
        self.assertEqual(options["dbname"], "integration_db")
        self.assertEqual(options["user"], "readonly-user")
        self.assertIn("default_transaction_read_only=on", options["options"])

    def test_export_connection_falls_back_to_deployed_db_settings(self):
        settings = {
            "DB_HOST": "shared-host",
            "DB_USER": "shared-user",
            "DB_PASSWORD": "shared-password",
            "EXPORT_POSTGRES_DB": "database-b",
        }

        with patch.dict(os.environ, settings, clear=True), patch.object(
            db.psycopg,
            "connect",
        ) as connect:
            db.get_export_connection()

        connection_options = connect.call_args.kwargs
        self.assertEqual(connection_options["host"], "shared-host")
        self.assertEqual(connection_options["dbname"], "database-b")
        self.assertEqual(connection_options["user"], "shared-user")

    def test_pooled_export_conninfo_carries_the_same_enforcement(self):
        # The interactive pickup path builds its pool from a conninfo rather than
        # calling psycopg.connect directly. Read-only enforcement is a property
        # of the connection, not of the caller, so it has to survive that route
        # exactly as it does the one-off one.
        settings = {
            "INTEGRATION_DB_HOST": "integration-host",
            "INTEGRATION_DB_NAME": "integration_db",
            "INTEGRATION_DB_USER": "readonly-user",
            "INTEGRATION_DB_PASSWORD": "readonly-password",
        }
        with patch.dict(os.environ, settings, clear=True):
            conninfo = db.export_conninfo(db.HTTP_EXPORT_STATEMENT_TIMEOUT_MS)

        self.assertIn("default_transaction_read_only=on", conninfo)
        self.assertIn("integration-host", conninfo)
        self.assertIn("integration_db", conninfo)
        self.assertIn("readonly-user", conninfo)
        self.assertIn(
            f"statement_timeout={db.HTTP_EXPORT_STATEMENT_TIMEOUT_MS}", conninfo
        )

    def test_pooled_and_one_off_export_settings_do_not_drift(self):
        settings = {
            "INTEGRATION_DB_HOST": "integration-host",
            "INTEGRATION_DB_NAME": "integration_db",
            "INTEGRATION_DB_USER": "readonly-user",
            "INTEGRATION_DB_PASSWORD": "readonly-password",
        }
        with patch.dict(os.environ, settings, clear=True), patch.object(
            db.psycopg, "connect"
        ) as connect:
            db.get_export_connection(1234)
            conninfo = db.export_conninfo(1234)

        direct = connect.call_args.kwargs
        for key in ("host", "dbname", "user", "options"):
            self.assertIn(str(direct[key]), conninfo, f"{key} differs between the two")

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

    def test_import_connection_never_falls_back_to_integration_settings(self):
        settings = {
            "DB_HOST": "database-a-host",
            "DB_NAME": "database-a",
            "DB_USER": "database-a-user",
            "DB_PASSWORD": "database-a-password",
        }

        with patch.dict(os.environ, settings, clear=True), self.assertRaises(KeyError):
            db.get_import_connection()


if __name__ == "__main__":
    unittest.main()
