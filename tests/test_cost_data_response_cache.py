import gzip
import json
import os
import unittest

from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

import function_app


class FakeRequest:
    def __init__(self, params=None, headers=None):
        self.params = params or {}
        self.headers = headers or {}


def request(include_comparison=True, headers=None):
    params = {
        "startDate": "2024-02-29",
        "endDate": "2024-02-29",
    }
    if include_comparison:
        params.update({
            "includeComparison": "true",
            "lyComparisonBasis": "sameDate",
        })
    return FakeRequest(params, headers)


def range_results():
    current_data = {
        "roomRevenue": [{"hotelName": "Hotel A", "stayDate": "2024-02-29"}],
    }
    comparison_data = {
        "roomRevenue": [{"hotelName": "Hotel A", "stayDate": "2023-02-28"}],
    }
    return {
        "current": (current_data, {"roomRevenue": 1}),
        "comparison": (comparison_data, {"roomRevenue": 1}),
    }


def decode(response):
    body = response.get_body()
    if response.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return json.loads(body)


class CostDataResponseCacheTests(unittest.TestCase):
    def setUp(self):
        function_app._cost_response_cache.clear()
        function_app._cost_response_inflight.clear()

    def tearDown(self):
        function_app._cost_response_cache.clear()
        function_app._cost_response_inflight.clear()

    def test_comparison_is_batched_and_the_rulebook_travels_once(self):
        with patch.object(
            function_app,
            "fetch_cost_publication_version",
            return_value=7,
        ), patch.object(
            function_app,
            "fetch_cost_data_ranges",
            return_value=range_results(),
        ) as fetch_ranges, patch.object(
            function_app,
            "fetch_cost_data",
        ) as fetch_single, patch.object(
            function_app,
            "fetch_all_cost_settings",
            return_value={"Hotel A": {"profile": {}}},
        ) as fetch_settings:
            response = function_app.cost_data_facts(request())

        self.assertEqual(response.status_code, 200)
        fetch_ranges.assert_called_once_with(
            (
                ("current", date(2024, 2, 29), date(2024, 2, 29)),
                ("comparison", date(2023, 2, 28), date(2023, 2, 28)),
            ),
            publication_version=7,
        )
        fetch_single.assert_not_called()
        fetch_settings.assert_called_once_with(publication_version=7)
        payload = decode(response)
        self.assertEqual(payload["publicationVersion"], 7)
        self.assertEqual(payload["comparison"]["parameters"]["startDate"], "2023-02-28")
        self.assertEqual(payload["comparison"]["data"]["roomRevenue"][0]["stayDate"], "2023-02-28")

    def test_spit_comparison_carries_lifecycle_as_of_datasets(self):
        spit_request = request()
        spit_request.params["comparisonMode"] = "spit"
        spit_data = {
            "roomRevenue": [{
                "hotelName": "Hotel A",
                "stayDate": "2023-02-28",
                "roomRevenueInclProducts1Net": "3000",
            }],
        }
        with patch.object(
            function_app, "fetch_cost_publication_version", return_value=7
        ), patch.object(
            function_app, "fetch_cost_data_ranges", return_value=range_results()
        ), patch.object(
            function_app, "fetch_cost_spit_data",
            return_value=(spit_data, {"roomRevenue": 1})
        ) as fetch_spit, patch.object(
            function_app, "fetch_supplement_status", return_value={"runId": 42}
        ), patch.object(
            function_app, "fetch_all_cost_settings", return_value={}
        ):
            response = function_app.cost_data_facts(spit_request)

        cutoff = function_app.shift_cost_comparison_date(
            datetime.now(ZoneInfo("Europe/Stockholm")).date(), "sameDate"
        )
        fetch_spit.assert_called_once_with(
            date(2023, 2, 28), date(2023, 2, 28), cutoff,
            7,
        )
        payload = decode(response)
        self.assertEqual(payload["comparison"]["parameters"]["mode"], "spit")
        self.assertEqual(payload["comparison"]["spit"]["cutoffDate"], cutoff.isoformat())
        self.assertEqual(payload["comparison"]["spit"]["method"], "lifecycle")
        self.assertEqual(payload["comparison"]["spit"]["data"], spit_data)
        self.assertEqual(
            payload["comparison"]["spit"]["rowCounts"], {"roomRevenue": 1}
        )

    def test_a_failed_spit_read_keeps_final_and_is_not_cached(self):
        spit_request = request()
        spit_request.params["comparisonMode"] = "spit"
        recovered = ({"roomRevenue": []}, {"roomRevenue": 0})
        with patch.object(
            function_app, "fetch_cost_publication_version", return_value=7
        ), patch.object(
            function_app, "fetch_cost_data_ranges", return_value=range_results()
        ), patch.object(
            function_app, "fetch_cost_spit_data",
            side_effect=[RuntimeError("source unavailable"), recovered]
        ) as fetch_spit, patch.object(
            function_app, "fetch_supplement_status", return_value={"runId": 42}
        ), patch.object(
            function_app, "fetch_all_cost_settings", return_value={}
        ):
            failed = decode(function_app.cost_data_facts(spit_request))
            healthy = decode(function_app.cost_data_facts(spit_request))

        self.assertFalse(failed["comparison"]["spit"]["available"])
        self.assertEqual(failed["comparison"]["data"], range_results()["comparison"][0])
        self.assertTrue(healthy["comparison"]["spit"]["available"])
        self.assertEqual(fetch_spit.call_count, 2)

    def test_complete_encoded_response_is_reused_for_one_publication(self):
        with patch.object(
            function_app,
            "fetch_cost_publication_version",
            return_value=7,
        ), patch.object(
            function_app,
            "fetch_cost_data_ranges",
            return_value=range_results(),
        ) as fetch_ranges, patch.object(
            function_app,
            "fetch_all_cost_settings",
            return_value={},
        ) as fetch_settings:
            first = function_app.cost_data_facts(
                request(headers={"Accept-Encoding": "gzip"})
            )
            second = function_app.cost_data_facts(
                request(headers={"Accept-Encoding": "gzip"})
            )

        self.assertEqual(fetch_ranges.call_count, 1)
        self.assertEqual(fetch_settings.call_count, 1)
        self.assertEqual(first.get_body(), second.get_body())
        self.assertEqual(first.headers["ETag"], second.headers["ETag"])
        self.assertEqual(first.headers["Content-Encoding"], "gzip")

    def test_validator_can_answer_before_the_server_has_response_bytes(self):
        with patch.object(
            function_app,
            "fetch_cost_publication_version",
            return_value=7,
        ), patch.object(
            function_app,
            "fetch_cost_data_ranges",
            return_value=range_results(),
        ), patch.object(
            function_app,
            "fetch_all_cost_settings",
            return_value={},
        ):
            first = function_app.cost_data_facts(request())

        function_app._cost_response_cache.clear()
        with patch.object(
            function_app,
            "fetch_cost_publication_version",
            return_value=7,
        ), patch.object(function_app, "fetch_cost_data_ranges") as fetch_ranges:
            repeat = function_app.cost_data_facts(
                request(headers={"If-None-Match": first.headers["ETag"]})
            )

        self.assertEqual(repeat.status_code, 304)
        fetch_ranges.assert_not_called()

    def test_new_publication_invalidates_response_and_dataset_caches(self):
        with patch.object(
            function_app,
            "fetch_cost_publication_version",
            side_effect=[7, 8],
        ), patch.object(
            function_app,
            "fetch_cost_data_ranges",
            return_value=range_results(),
        ) as fetch_ranges, patch.object(
            function_app,
            "fetch_all_cost_settings",
            return_value={},
        ):
            before = function_app.cost_data_facts(request())
            after = function_app.cost_data_facts(request())

        self.assertEqual(fetch_ranges.call_count, 2)
        self.assertNotEqual(before.headers["ETag"], after.headers["ETag"])

    def test_degraded_settings_response_is_not_retained(self):
        with patch.object(
            function_app,
            "fetch_cost_publication_version",
            return_value=7,
        ), patch.object(
            function_app,
            "fetch_cost_data_ranges",
            return_value=range_results(),
        ) as fetch_ranges, patch.object(
            function_app,
            "fetch_all_cost_settings",
            side_effect=[RuntimeError("temporary"), {}],
        ):
            degraded = function_app.cost_data_facts(request())
            recovered = function_app.cost_data_facts(request())

        self.assertEqual(decode(degraded)["costSettings"], {})
        self.assertEqual(decode(recovered)["costSettings"], {})
        self.assertEqual(fetch_ranges.call_count, 2)


if __name__ == "__main__":
    unittest.main()
