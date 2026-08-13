import os
import gzip
import json
import unittest

from datetime import datetime, timezone
from unittest.mock import patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "integration_db")
os.environ.setdefault("DB_USER", "readonly")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

import function_app


class FakeRequest:
    def __init__(self, params=None, body=None, headers=None):
        self.params = params or {}
        self._body = body or {}
        self.headers = headers or {}

    def get_json(self):
        return self._body


class FakeTimer:
    past_due = False


class SupplementTriggerTests(unittest.TestCase):
    def test_disabled_grid_does_not_touch_any_database_service(self):
        with patch.dict(os.environ, {"SUPPLEMENT_LIVE_ENABLED": "false"}), patch.object(
            function_app,
            "fetch_supplement_grid",
        ) as fetch:
            response = function_app.supplement_grid(FakeRequest())
        self.assertEqual(response.status_code, 503)
        fetch.assert_not_called()

    def test_grid_validates_and_forwards_normalized_parameters(self):
        payload = {"runId": 9, "dataAsOf": "2026-08-12", "dates": [], "rows": []}
        request = FakeRequest(params={
            "startDate": "2026-08-10",
            "endDate": "2026-08-12",
            "mode": "comparison",
            "hotelCodes": "a,b",
            "lyComparisonBasis": "sameWeekday",
            "inventoryBasis": "physical",
        })
        with patch.dict(os.environ, {"SUPPLEMENT_LIVE_ENABLED": "true"}), patch.object(
            function_app,
            "fetch_supplement_grid",
            return_value=payload,
        ) as fetch:
            response = function_app.supplement_grid(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn("ETag", response.headers)
        fetch.assert_called_once()
        arguments = fetch.call_args.kwargs
        self.assertEqual(arguments["hotel_codes"], ["a", "b"])
        self.assertEqual(arguments["ly_comparison_basis"], "sameWeekday")
        self.assertEqual(arguments["inventory_basis"], "physical")

    def test_authenticated_import_forwards_repair_dates(self):
        expected = {"status": "published", "runId": 4}
        request = FakeRequest(body={
            "mode": "repair",
            "snapshotFrom": "2026-08-10",
            "snapshotTo": "2026-08-12",
        })
        with patch.dict(os.environ, {"SUPPLEMENT_LIVE_ENABLED": "true"}), patch.object(
            function_app,
            "sync_supplement",
            return_value=expected,
        ) as sync:
            response = function_app.supplement_import(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sync.call_args.args[0], "repair")
        self.assertEqual(sync.call_args.args[1].isoformat(), "2026-08-10")
        self.assertEqual(sync.call_args.args[2].isoformat(), "2026-08-12")

    def test_daily_timer_runs_only_when_enabled(self):
        with patch.dict(os.environ, {"SUPPLEMENT_LIVE_ENABLED": "true"}), patch.object(
            function_app,
            "sync_supplement",
            return_value={"status": "published"},
        ) as sync, patch.object(function_app, "supplement_timer_due", return_value=True):
            function_app.supplement_data_timer(FakeTimer())
        sync.assert_called_once_with("delta")

    def test_stockholm_timer_selects_one_utc_candidate_across_dst(self):
        cases = (
            (datetime(2026, 1, 15, 1, 15, tzinfo=timezone.utc), True),
            (datetime(2026, 1, 15, 0, 15, tzinfo=timezone.utc), False),
            (datetime(2026, 7, 15, 0, 15, tzinfo=timezone.utc), True),
            (datetime(2026, 7, 15, 1, 15, tzinfo=timezone.utc), False),
            # First 02:15 wins when the clock repeats during autumn fallback.
            (datetime(2026, 10, 25, 0, 15, tzinfo=timezone.utc), True),
            (datetime(2026, 10, 25, 1, 15, tzinfo=timezone.utc), False),
            # Spring's nonexistent 02:15 runs at the first valid instant after it.
            (datetime(2026, 3, 29, 1, 15, tzinfo=timezone.utc), True),
        )
        with patch.dict(os.environ, {"SUPPLEMENT_TIME_ZONE": "Europe/Stockholm"}):
            for timestamp, expected in cases:
                with self.subTest(timestamp=timestamp):
                    self.assertEqual(function_app.supplement_timer_due(timestamp), expected)

    def test_cached_response_compresses_when_requested(self):
        payload = {"runId": 7, "status": "available", "rows": [1, 2, 3]}
        request = FakeRequest(headers={"Accept-Encoding": "gzip"})
        response = function_app.supplement_cached_response(
            request, payload, "normalized-key"
        )
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertEqual(json.loads(gzip.decompress(response.get_body())), payload)
        self.assertEqual(response.headers["Vary"], "Accept-Encoding")


if __name__ == "__main__":
    unittest.main()
