import sys
import unittest

from pathlib import Path
from unittest.mock import patch

import TimerFunc
import costdata


APP_ROOT = str(Path(__file__).resolve().parent.parent)


class FakeTimer:
    past_due = False


class FakeRequest:
    def __init__(self, body):
        self.body = body

    def get_json(self):
        return self.body


class CostDataTriggerTests(unittest.TestCase):
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
