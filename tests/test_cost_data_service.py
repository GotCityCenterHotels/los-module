import os
import threading
import time
import unittest

from datetime import date, datetime, timezone
from decimal import Decimal


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from services import cost_data_service


cost_data_service.cost_pool.close()


class FakeCursor:
    """Answers by query, not by call order.

    The datasets are fetched concurrently, so nothing guarantees which query
    runs first - and a fake that hands back the next result set in sequence
    would quietly pair each dataset with somebody else's rows.
    """

    def __init__(self, results_by_query, executions, lock):
        self.results_by_query = results_by_query
        self.executions = executions
        self.lock = lock
        self.current = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters):
        with self.lock:
            self.executions.append((query, parameters))
        self.current = self.results_by_query[query]

    def fetchall(self):
        return self.current


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, row_factory=None):
        return self.fake_cursor


class FakePool:
    """One cursor per checkout, because the real pool hands out one per caller.

    A single shared cursor would serialise what the service now runs in
    parallel, and would let one worker's fetchall see another's rows.
    """

    def __init__(self, results_by_query):
        self.results_by_query = results_by_query
        self.executions = []
        self.connection_count = 0
        self.lock = threading.Lock()
        self.max_size = 4

    def connection(self):
        with self.lock:
            self.connection_count += 1
        return FakeConnection(
            FakeCursor(self.results_by_query, self.executions, self.lock)
        )


class CostDataServiceTests(unittest.TestCase):
    def setUp(self):
        cost_data_service._result_cache.clear()
        cost_data_service._result_inflight.clear()

    def tearDown(self):
        cost_data_service._result_cache.clear()
        cost_data_service._result_inflight.clear()

    def test_all_datasets_are_date_bounded_and_json_safe(self):
        # Keyed by dataset rather than positional. The fake used to hand back
        # result sets in order, so adding a query to COST_DATA_QUERIES gave one
        # dataset another's rows - or, once the list outgrew the fixture, a bare
        # StopIteration out of the middle of the service.
        result_sets = {
            "arrivalsDepartures": [{
                "hotel_name": "Hotel A",
                "stay_date": date(2026, 1, 2),
                "total_arrivals": 3,
                "total_departures": 2,
                "last_updated_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
            }],
            "roomRevenue": [{
                "hotel_name": "Hotel A",
                "stay_date": date(2026, 1, 2),
                "amount_currency": "SEK",
                "room_revenue_incl_products_1_net": Decimal("123.45"),
                "room_revenue_excl_products_1_net": Decimal("100.00"),
                "product_revenue_1_net": Decimal("23.45"),
                "last_updated_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
            }],
            "cleaningDepartures": [{
                "hotel_name": "Hotel A",
                "category_name": "Double",
                "occupancy": 2,
                "departures": 4,
                "last_updated_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
            }],
            "distributionRates": [{
                "hotel_name": "Hotel A",
                "stay_date": date(2026, 1, 2),
                "mix_revenue": Decimal("500.00"),
                "matched_revenue": Decimal("400.00"),
                "matched_percent": Decimal("12.5000"),
                "last_updated_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
            }],
        }
        results_by_query = {
            query: result_sets.get(dataset, [])
            for dataset, query in cost_data_service.COST_DATA_QUERIES.items()
        }
        original_pool = cost_data_service.cost_pool
        original_ensure = cost_data_service.ensure_cost_settings_schema
        fake_pool = FakePool(results_by_query)
        cost_data_service.cost_pool = fake_pool
        # Recorded, not merely stubbed: two of these datasets read tables that
        # migration 016 creates, and this read runs before the one that used to be
        # the only thing applying migrations on this route.
        ensured = []
        cost_data_service.ensure_cost_settings_schema = lambda: ensured.append(
            len(fake_pool.executions)
        )

        try:
            datasets, row_counts = cost_data_service.fetch_cost_data(
                date(2026, 1, 1),
                date(2026, 1, 31),
            )
        finally:
            cost_data_service.cost_pool = original_pool
            cost_data_service.ensure_cost_settings_schema = original_ensure

        # Once, and before a single dataset query ran - otherwise a Database A
        # without 016 answers UndefinedTable for the whole page, taking down the
        # five datasets that were fine with the two that were not.
        self.assertEqual(ensured, [0])

        self.assertEqual(set(datasets), {
            "arrivalsDepartures", "breakfast", "parking", "roomRevenue", "payments",
            "cleaningDepartures", "distributionRates",
        })
        self.assertEqual(row_counts["arrivalsDepartures"], 1)
        self.assertEqual(datasets["arrivalsDepartures"][0]["stayDate"], "2026-01-02")
        self.assertEqual(
            datasets["roomRevenue"][0]["roomRevenueInclProducts1Net"],
            "123.45",
        )
        # The mixes travel in the same envelope and through the same JSON
        # coercion: a Decimal percentage reaching the browser as a float would
        # round differently there than the statement rounds here.
        self.assertEqual(datasets["cleaningDepartures"][0]["categoryName"], "Double")
        self.assertEqual(datasets["cleaningDepartures"][0]["occupancy"], 2)
        self.assertNotIn("stayDate", datasets["cleaningDepartures"][0])
        self.assertEqual(datasets["distributionRates"][0]["matchedPercent"], "12.5000")
        self.assertEqual(datasets["distributionRates"][0]["mixRevenue"], "500.00")

        self.assertEqual(
            len(fake_pool.executions), len(cost_data_service.COST_DATA_QUERIES)
        )
        for query, parameters in fake_pool.executions:
            self.assertIn("stay_date BETWEEN", query)
            self.assertEqual(parameters["start_date"], date(2026, 1, 1))
            self.assertEqual(parameters["end_date"], date(2026, 1, 31))

        distribution_query = cost_data_service.COST_DATA_QUERIES[
            "distributionRates"
        ]
        self.assertIn("priced AS MATERIALIZED", distribution_query)
        self.assertNotIn("IS NOT DISTINCT FROM mix", distribution_query)
        for field in ("origin", "travel_agency", "rate_name"):
            self.assertIn(
                f"coalesce(priced.{field}, '') = coalesce(mix.{field}, '')",
                distribution_query,
            )
            self.assertIn(
                f"(priced.{field} IS NULL) = (mix.{field} IS NULL)",
                distribution_query,
            )

        cleaning_query = cost_data_service.COST_DATA_QUERIES[
            "cleaningDepartures"
        ]
        self.assertIn(
            "GROUP BY hotel.hotel_name, fact.category_name, fact.occupancy",
            cleaning_query,
        )
        self.assertNotIn("GROUP BY hotel.hotel_name, stay_date", cleaning_query)

    def test_identical_requests_share_the_cached_result(self):
        calls = []
        result = ({"roomRevenue": [{"hotelName": "Hotel A"}]}, {"roomRevenue": 1})

        original = cost_data_service._fetch_cost_data_uncached
        cost_data_service._fetch_cost_data_uncached = lambda *arguments: (
            calls.append(arguments) or result
        )
        try:
            first = cost_data_service.fetch_cost_data(
                date(2026, 1, 1), date(2026, 1, 31)
            )
            second = cost_data_service.fetch_cost_data(
                date(2026, 1, 1), date(2026, 1, 31)
            )
        finally:
            cost_data_service._fetch_cost_data_uncached = original

        self.assertIs(first, result)
        self.assertIs(second, result)
        self.assertEqual(len(calls), 1)

    def test_publication_version_invalidates_a_range_cache_entry(self):
        calls = []
        original = cost_data_service._fetch_cost_data_uncached
        cost_data_service._fetch_cost_data_uncached = lambda *arguments: (
            calls.append(arguments)
            or ({"roomRevenue": []}, {"roomRevenue": 0})
        )
        try:
            arguments = (date(2026, 1, 1), date(2026, 1, 31))
            cost_data_service.fetch_cost_data(*arguments, publication_version=4)
            cost_data_service.fetch_cost_data(*arguments, publication_version=4)
            cost_data_service.fetch_cost_data(*arguments, publication_version=5)
        finally:
            cost_data_service._fetch_cost_data_uncached = original

        self.assertEqual(len(calls), 2)

    def test_two_ranges_run_together_and_populate_the_single_range_cache(self):
        calls = []
        ranges = (
            ("current", date(2026, 1, 1), date(2026, 1, 31)),
            ("comparison", date(2025, 1, 1), date(2025, 1, 31)),
        )

        def build(start_date, end_date):
            calls.append((start_date, end_date))
            return (
                {"roomRevenue": [{"startDate": start_date.isoformat()}]},
                {"roomRevenue": 1},
            )

        original = cost_data_service._fetch_cost_data_uncached
        cost_data_service._fetch_cost_data_uncached = build

        try:
            result = cost_data_service.fetch_cost_data_ranges(
                ranges,
                publication_version=8,
            )
            current_again = cost_data_service.fetch_cost_data(
                date(2026, 1, 1),
                date(2026, 1, 31),
                publication_version=8,
            )
        finally:
            cost_data_service._fetch_cost_data_uncached = original

        self.assertEqual(set(result), {"current", "comparison"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            {start for start, _end in calls},
            {date(2026, 1, 1), date(2025, 1, 1)},
        )
        self.assertIs(current_again, result["current"])

    def test_concurrent_identical_misses_run_the_queries_once(self):
        started = threading.Event()
        release = threading.Event()
        calls = []
        result = ({"payments": []}, {"payments": 0})

        def build(*arguments):
            calls.append(arguments)
            started.set()
            release.wait(timeout=2)
            return result

        original = cost_data_service._fetch_cost_data_uncached
        cost_data_service._fetch_cost_data_uncached = build
        answers = []
        try:
            arguments = (date(2026, 2, 1), date(2026, 2, 28))
            first = threading.Thread(
                target=lambda: answers.append(cost_data_service.fetch_cost_data(*arguments))
            )
            second = threading.Thread(
                target=lambda: answers.append(cost_data_service.fetch_cost_data(*arguments))
            )
            first.start()
            self.assertTrue(started.wait(timeout=1))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(timeout=2)
            second.join(timeout=2)
        finally:
            release.set()
            cost_data_service._fetch_cost_data_uncached = original

        self.assertEqual(len(calls), 1)
        self.assertEqual(answers, [result, result])

    def test_a_failed_build_is_not_cached(self):
        calls = []

        def build(*arguments):
            calls.append(arguments)
            if len(calls) == 1:
                raise RuntimeError("temporary database failure")
            return ({"breakfast": []}, {"breakfast": 0})

        original = cost_data_service._fetch_cost_data_uncached
        cost_data_service._fetch_cost_data_uncached = build
        arguments = (date(2026, 3, 1), date(2026, 3, 31))
        try:
            with self.assertRaisesRegex(RuntimeError, "temporary database failure"):
                cost_data_service.fetch_cost_data(*arguments)
            recovered = cost_data_service.fetch_cost_data(*arguments)
        finally:
            cost_data_service._fetch_cost_data_uncached = original

        self.assertEqual(recovered, ({"breakfast": []}, {"breakfast": 0}))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
