import os
import threading
import unittest

from datetime import date


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from queries.cost_spit import COST_SPIT_DATASETS, COST_SPIT_SQL
from services import cost_data_service


class FakeCursor:
    def __init__(self, executions, lock, rows):
        self.executions = executions
        self.lock = lock
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters):
        with self.lock:
            self.executions.append((query, parameters))

    def fetchall(self):
        return self.rows


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
    def __init__(self, rows=None):
        self.executions = []
        self.lock = threading.Lock()
        self.max_size = 4
        self.rows = rows or []

    def connection(self):
        return FakeConnection(FakeCursor(self.executions, self.lock, self.rows))


class CostSpitSourceTests(unittest.TestCase):
    def setUp(self):
        cost_data_service._result_cache.clear()
        cost_data_service._result_inflight.clear()

    def tearDown(self):
        cost_data_service._result_cache.clear()
        cost_data_service._result_inflight.clear()

    def test_the_consolidated_read_uses_the_los_lifecycle_boundary(self):
        normalized = " ".join(COST_SPIT_SQL.lower().split())
        self.assertIn("item.created_utc::date <= %(cutoff_date)s", normalized)
        self.assertIn(
            "item.canceled_utc is null or item.canceled_utc::date > %(cutoff_date)s",
            normalized,
        )
        # A final-state predicate would drop the exact records SPIT must retain:
        # bookings/items cancelled after the cutoff.
        self.assertNotIn("item.canceled_utc is null and", normalized)

    def test_reservation_derived_facts_filter_both_lifecycle_levels(self):
        normalized = " ".join(COST_SPIT_SQL.lower().split())
        self.assertIn(
            "reservation_created_utc::date <= %(cutoff_date)s",
            normalized,
        )
        self.assertIn(
            "reservation_cancelled_utc is null or "
            "reservation_cancelled_utc::date > %(cutoff_date)s",
            normalized,
        )

    def test_the_full_range_scans_items_twice_not_once_per_dataset(self):
        normalized = " ".join(COST_SPIT_SQL.lower().split())
        self.assertEqual(normalized.count("order_item_current item"), 2)
        self.assertIn("scoped_items as materialized", normalized)
        self.assertIn("eligible_nights as materialized", normalized)
        for dataset in COST_SPIT_DATASETS:
            self.assertIn(f"'{dataset.lower()}'", normalized)

    def test_an_empty_lifecycle_result_is_available_not_missing(self):
        fake_pool = FakePool()
        original_pool = cost_data_service.source_pool
        cost_data_service.source_pool = fake_pool
        try:
            datasets, counts = cost_data_service.fetch_cost_spit_data(
                date(2025, 10, 1),
                date(2025, 10, 31),
                date(2025, 8, 19),
                publication_version=7,
            )
        finally:
            cost_data_service.source_pool = original_pool

        self.assertEqual(set(datasets), set(COST_SPIT_DATASETS))
        self.assertTrue(all(rows == [] for rows in datasets.values()))
        self.assertTrue(all(count == 0 for count in counts.values()))
        self.assertEqual(len(fake_pool.executions), 1)
        for _query, parameters in fake_pool.executions:
            self.assertEqual(parameters["cutoff_date"], date(2025, 8, 19))

    def test_tagged_rows_are_restored_to_the_existing_json_shape(self):
        fake_pool = FakePool(rows=[{
            "dataset": "roomRevenue",
            "payload": {
                "hotel_name": "Hotel A",
                "stay_date": "2025-07-01",
                "room_revenue_incl_products_1_net": "123",
            },
        }])
        original_pool = cost_data_service.source_pool
        cost_data_service.source_pool = fake_pool
        try:
            datasets, counts = cost_data_service.fetch_cost_spit_data(
                date(2025, 7, 1),
                date(2025, 7, 31),
                date(2025, 8, 19),
                publication_version=8,
            )
        finally:
            cost_data_service.source_pool = original_pool

        self.assertEqual(datasets["roomRevenue"], [{
            "hotelName": "Hotel A",
            "stayDate": "2025-07-01",
            "roomRevenueInclProducts1Net": "123",
        }])
        self.assertEqual(counts["roomRevenue"], 1)


if __name__ == "__main__":
    unittest.main()
