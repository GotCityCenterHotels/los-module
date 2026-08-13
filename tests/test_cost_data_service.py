import os
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
    def __init__(self, result_sets):
        self.result_sets = iter(result_sets)
        self.current = []
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters):
        self.executions.append((query, parameters))
        self.current = next(self.result_sets)

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
    def __init__(self, result_sets):
        self.cursor = FakeCursor(result_sets)

    def connection(self):
        return FakeConnection(self.cursor)


class CostDataServiceTests(unittest.TestCase):
    def test_all_datasets_are_date_bounded_and_json_safe(self):
        result_sets = [
            [{
                "hotel_name": "Hotel A",
                "stay_date": date(2026, 1, 2),
                "total_arrivals": 3,
                "total_departures": 2,
                "last_updated_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
            }],
            [],
            [],
            [{
                "hotel_name": "Hotel A",
                "stay_date": date(2026, 1, 2),
                "amount_currency": "SEK",
                "room_revenue_incl_products_1_net": Decimal("123.45"),
                "room_revenue_excl_products_1_net": Decimal("100.00"),
                "product_revenue_1_net": Decimal("23.45"),
                "last_updated_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
            }],
            [],
        ]
        original_pool = cost_data_service.cost_pool
        fake_pool = FakePool(result_sets)
        cost_data_service.cost_pool = fake_pool

        try:
            datasets, row_counts = cost_data_service.fetch_cost_data(
                date(2026, 1, 1),
                date(2026, 1, 31),
            )
        finally:
            cost_data_service.cost_pool = original_pool

        self.assertEqual(set(datasets), {
            "arrivalsDepartures", "breakfast", "parking", "roomRevenue", "payments"
        })
        self.assertEqual(row_counts["arrivalsDepartures"], 1)
        self.assertEqual(datasets["arrivalsDepartures"][0]["stayDate"], "2026-01-02")
        self.assertEqual(
            datasets["roomRevenue"][0]["roomRevenueInclProducts1Net"],
            "123.45",
        )
        self.assertEqual(len(fake_pool.cursor.executions), 5)
        for query, parameters in fake_pool.cursor.executions:
            self.assertIn("stay_date BETWEEN", query)
            self.assertEqual(parameters["start_date"], date(2026, 1, 1))
            self.assertEqual(parameters["end_date"], date(2026, 1, 31))


if __name__ == "__main__":
    unittest.main()
