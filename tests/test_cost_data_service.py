import os
import threading
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
        self.lock = threading.Lock()
        self.max_size = 4

    def connection(self):
        return FakeConnection(
            FakeCursor(self.results_by_query, self.executions, self.lock)
        )


class CostDataServiceTests(unittest.TestCase):
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
                "stay_date": date(2026, 1, 2),
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
        self.assertEqual(datasets["distributionRates"][0]["matchedPercent"], "12.5000")
        self.assertEqual(datasets["distributionRates"][0]["mixRevenue"], "500.00")

        self.assertEqual(
            len(fake_pool.executions), len(cost_data_service.COST_DATA_QUERIES)
        )
        for query, parameters in fake_pool.executions:
            self.assertIn("stay_date BETWEEN", query)
            self.assertEqual(parameters["start_date"], date(2026, 1, 1))
            self.assertEqual(parameters["end_date"], date(2026, 1, 31))


if __name__ == "__main__":
    unittest.main()
