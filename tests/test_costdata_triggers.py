import sys
import unittest

from pathlib import Path
from unittest.mock import patch

import TimerFunc
import costdata
from shared import pipeline


APP_ROOT = str(Path(__file__).resolve().parent.parent)


class FakeTimer:
    past_due = False


class FakeRequest:
    def __init__(self, body):
        self.body = body

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
        ) as transfer:
            result = pipeline.run_dataset("properties")

        ensure_schema.assert_called_once_with()
        transfer.assert_called_once_with(
            export_sql_file="export/cost_properties.sql",
            import_sql_file="import/upsert_cost_properties.sql",
            batch_size=5000,
        )
        self.assertEqual(result, {"dataset": "properties", **expected})

    def test_timer_imports_shared_pipeline_from_application_root(self):
        expected = {"status": "success", "results": []}

        with patch("shared.pipeline.run_all_datasets", return_value=expected) as run_all:
            TimerFunc.main(FakeTimer())

        run_all.assert_called_once_with()
        self.assertIn(APP_ROOT, sys.path)

    def test_manual_trigger_runs_a_selected_dataset(self):
        expected = {"dataset": "parking", "export_rows": 2, "import_rows": 2}

        with patch("shared.pipeline.run_dataset", return_value=expected) as run_one:
            response = costdata.main(FakeRequest({"dataset": "parking"}))

        run_one.assert_called_once_with("parking")
        self.assertEqual(response.status_code, 200)
        self.assertIn('"dataset": "parking"', response.get_body().decode())


if __name__ == "__main__":
    unittest.main()
