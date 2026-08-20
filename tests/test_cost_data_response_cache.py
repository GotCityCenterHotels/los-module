import gzip
import json
import os
import unittest

from datetime import date, datetime, timedelta
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

from services.cost_data_service import CostSpitSnapshot


def snapshot(data, cutoff=None, stale_days=0):
    """A published SPIT reading, carried as the serialized bytes it is stored as."""
    return CostSpitSnapshot(
        json.dumps(data, separators=(",", ":")),
        {dataset: len(rows) for dataset, rows in data.items()},
        cutoff or function_app.shift_cost_comparison_date(
            datetime.now(ZoneInfo("Europe/Stockholm")).date(), "sameDate"
        ),
        stale_days,
    )


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
            return_value=snapshot(spit_data)
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
            7, "sameDate",
        )
        payload = decode(response)
        self.assertEqual(payload["comparison"]["parameters"]["mode"], "spit")
        self.assertEqual(payload["comparison"]["spit"]["cutoffDate"], cutoff.isoformat())
        self.assertEqual(payload["comparison"]["spit"]["method"], "lifecycle")
        self.assertEqual(payload["comparison"]["spit"]["data"], spit_data)
        self.assertEqual(
            payload["comparison"]["spit"]["rowCounts"], {"roomRevenue": 1}
        )
        self.assertEqual(payload["comparison"]["spit"]["staleDays"], 0)
        self.assertEqual(
            payload["comparison"]["spit"]["requestedCutoffDate"], cutoff.isoformat()
        )

    def test_a_stale_publication_is_served_and_names_its_own_cutoff(self):
        """A snapshot from an earlier night is a true point in time, just an
        earlier one. Withholding SPIT until tonight's import lands would blank
        the column between midnight and the import, and all day after a failed
        one, which is worse than showing a reading that says how old it is."""
        spit_request = request()
        spit_request.params["comparisonMode"] = "spit"
        requested = function_app.shift_cost_comparison_date(
            datetime.now(ZoneInfo("Europe/Stockholm")).date(), "sameDate"
        )
        published = requested - timedelta(days=2)
        with patch.object(
            function_app, "fetch_cost_publication_version", return_value=7
        ), patch.object(
            function_app, "fetch_cost_data_ranges", return_value=range_results()
        ), patch.object(
            function_app, "fetch_cost_spit_data",
            return_value=snapshot(
                {"roomRevenue": [{"hotelName": "Hotel A"}]},
                cutoff=published,
                stale_days=2,
            )
        ), patch.object(
            function_app, "fetch_all_cost_settings", return_value={}
        ):
            payload = decode(function_app.cost_data_facts(spit_request))

        spit = payload["comparison"]["spit"]
        self.assertTrue(spit["available"])
        self.assertEqual(spit["cutoffDate"], published.isoformat())
        self.assertEqual(spit["requestedCutoffDate"], requested.isoformat())
        self.assertEqual(spit["staleDays"], 2)

    def test_the_spliced_body_is_byte_identical_to_encoding_the_payload(self):
        """The SPIT datasets reach the body as stored text rather than being
        decoded and re-encoded, so the thing that has to be proved is that the
        shortcut produces the same bytes the long way round would have."""
        spit_request = request()
        spit_request.params["comparisonMode"] = "spit"
        spit_data = {
            "roomRevenue": [
                {"hotelName": "Hotel A", "stayDate": "2023-02-28", "net": "3000"},
                {"hotelName": "Hotel B", "stayDate": "2023-02-28", "net": "12.5"},
            ],
            "payments": [],
        }
        with patch.object(
            function_app, "fetch_cost_publication_version", return_value=7
        ), patch.object(
            function_app, "fetch_cost_data_ranges", return_value=range_results()
        ), patch.object(
            function_app, "fetch_cost_spit_data", return_value=snapshot(spit_data)
        ), patch.object(
            function_app, "fetch_all_cost_settings", return_value={}
        ):
            payload = decode(function_app.cost_data_facts(spit_request))

        self.assertEqual(payload["comparison"]["spit"]["data"], spit_data)
        self.assertEqual(
            payload["comparison"]["spit"]["rowCounts"],
            {"roomRevenue": 2, "payments": 0},
        )

    def test_a_failed_spit_read_keeps_final_and_is_not_cached(self):
        spit_request = request()
        spit_request.params["comparisonMode"] = "spit"
        recovered = snapshot({"roomRevenue": []})
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

    def test_a_degraded_body_carries_no_validator_and_no_freshness(self):
        """The server refusing to keep a body must stop the browser keeping it.

        Retaining it server-side was already skipped, but the response still
        went out with the ETag a healthy body would have carried - the validator
        is built from the publication version and the dates, and knows nothing
        about a rulebook lookup that timed out. So one unlucky client kept a
        statement with no Cost Input configuration, revalidated inside the
        window, and was answered 304 before any query ran: stale until the
        publication moved, while every other client got a healthy body under the
        identical ETag.
        """
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
            side_effect=[RuntimeError("temporary"), {}],
        ):
            degraded = function_app.cost_data_facts(request())
            healthy = function_app.cost_data_facts(request())

        self.assertNotIn("ETag", degraded.headers)
        self.assertEqual(degraded.headers["Cache-Control"], "no-store")
        # The healthy answer is still fully revalidatable, so the fix costs
        # nothing on the path that matters.
        self.assertIn("ETag", healthy.headers)
        self.assertIn("max-age", healthy.headers["Cache-Control"])
        # And the ETag the degraded body would have carried is the healthy one,
        # which is exactly why it could not be allowed to carry it.
        self.assertEqual(
            degraded.headers.get("ETag", None), None
        )

    def test_a_waiter_on_a_degraded_build_is_also_told_not_to_keep_it(self):
        """Single-flight shares the bytes, so it must share the cacheability."""
        entry = (b'{"a":1}', gzip.compress(b'{"a":1}'))
        cache = function_app.VersionedResponseCache("probe", 60, 4)
        pending = function_app.Future()
        cache.inflight[("k",)] = pending
        pending.set_result((entry, False))

        response = cache.respond(
            FakeRequest(), ("k",), 'W/"probe-x"', lambda: (None, True)
        )

        self.assertNotIn("ETag", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_api_responses_carry_nosniff(self):
        """staticwebapp.config.json globalHeaders do not reach /api/*."""
        with patch.object(
            function_app,
            "fetch_cost_publication_version",
            return_value=7,
        ), patch.object(
            function_app,
            "fetch_cost_data_ranges",
            return_value=range_results(),
        ), patch.object(
            function_app, "fetch_all_cost_settings", return_value={}
        ):
            ok = function_app.cost_data_facts(request())

        self.assertEqual(ok.headers["X-Content-Type-Options"], "nosniff")
        error = function_app.json_response({"error": "no"}, 400)
        self.assertEqual(error.headers["X-Content-Type-Options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
