import os
import unittest

from unittest.mock import patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

import function_app
from cost_database import cost_pool
from database import pool
from shared import pipeline


cost_pool.close()
pool.close()


class FakeTimer:
    past_due = False


class FakeRequest:
    def __init__(self, body, params=None):
        self.body = body
        self.params = params or {}

    def get_json(self):
        return self.body


class CostDataTriggerTests(unittest.TestCase):
    def test_property_sync_creates_database_a_target_before_transfer(self):
        expected = {"export_rows": 8, "import_rows": 8}

        with patch(
            "services.cost_schema_service.ensure_cost_settings_schema",
        ) as ensure_schema, patch.object(
            pipeline,
            "transfer_dataset",
            return_value=expected,
        ) as transfer, patch(
            "cost_database.cost_pool.connection",
        ) as pool_connection:
            cursor = pool_connection.return_value.__enter__.return_value.cursor
            cursor = cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = (8,)
            result = pipeline.run_dataset("properties")

        ensure_schema.assert_called_once_with()
        transfer.assert_called_once_with(
            export_sql_file="export/cost_properties.sql",
            import_sql_file="import/upsert_cost_properties.sql",
            batch_size=5000,
        )
        self.assertEqual(result, {
            "dataset": "properties",
            **expected,
            "verified_rows": 8,
        })

    def test_property_sync_rejects_an_empty_source_result(self):
        with patch(
            "services.cost_schema_service.ensure_cost_settings_schema",
        ), patch.object(
            pipeline,
            "transfer_dataset",
            return_value={"export_rows": 0, "import_rows": 0},
        ), self.assertRaisesRegex(RuntimeError, "returned no GCCH properties"):
            pipeline.run_dataset("properties")

    def test_timer_runs_every_dataset(self):
        expected = {"status": "success", "results": []}

        with patch.object(
            function_app,
            "run_all_datasets",
            return_value=expected,
        ) as run_all:
            function_app.cost_data_timer(FakeTimer())

        run_all.assert_called_once_with()

    def test_manual_trigger_runs_a_selected_dataset(self):
        expected = {"dataset": "parking", "export_rows": 2, "import_rows": 2}

        with patch.object(
            function_app,
            "run_dataset",
            return_value=expected,
        ) as run_one:
            response = function_app.cost_data_import(
                FakeRequest({"dataset": "parking"})
            )

        run_one.assert_called_once_with("parking")
        self.assertEqual(response.status_code, 200)
        self.assertIn('"dataset":"parking"', response.get_body().decode())
        self.assertIn('"status":"success"', response.get_body().decode())

    def test_manual_trigger_defaults_to_all_datasets(self):
        expected = {"status": "success", "results": []}

        with patch.object(
            function_app,
            "run_all_datasets",
            return_value=expected,
        ) as run_all:
            response = function_app.cost_data_import(FakeRequest({}))

        run_all.assert_called_once_with()
        self.assertEqual(response.status_code, 200)

    def test_manual_trigger_returns_property_validation_failure(self):
        with patch.object(
            function_app,
            "run_dataset",
            side_effect=RuntimeError("enterprise_current returned no GCCH properties"),
        ):
            response = function_app.cost_data_import(
                FakeRequest({"dataset": "properties"})
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn(
            "enterprise_current returned no GCCH properties",
            response.get_body().decode(),
        )

    def test_v2_function_app_registers_manual_and_timer_triggers(self):
        registered_functions = function_app.app.get_functions()
        function_names = {
            registered.get_function_name()
            for registered in registered_functions
        }
        routes = {
            binding.get_dict_repr().get("route")
            for registered in registered_functions
            for binding in registered.get_bindings()
            if binding.get_dict_repr().get("type") == "httpTrigger"
        }

        self.assertIn("CostDataImport", function_names)
        self.assertIn("CostDataTimer", function_names)
        self.assertIn("SupplementDataImport", function_names)
        self.assertIn("SupplementDataTimer", function_names)
        self.assertIn("costdata/properties", routes)
        self.assertIn("costdata/settings/{enterprise_id}", routes)
        self.assertNotIn("costdata/settings/hotels", routes)


if __name__ == "__main__":
    unittest.main()
