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

from queries.cost_spit import COST_SPIT_QUERIES
from services import cost_data_service


class FakeCursor:
    def __init__(self, executions, lock):
        self.executions = executions
        self.lock = lock

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters):
        with self.lock:
            self.executions.append((query, parameters))

    def fetchall(self):
        return []


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
    def __init__(self):
        self.executions = []
        self.lock = threading.Lock()
        self.max_size = 4

    def connection(self):
        return FakeConnection(FakeCursor(self.executions, self.lock))


class CostSpitSourceTests(unittest.TestCase):
    def setUp(self):
        cost_data_service._result_cache.clear()
        cost_data_service._result_inflight.clear()

    def tearDown(self):
        cost_data_service._result_cache.clear()
        cost_data_service._result_inflight.clear()

    def test_every_item_dataset_uses_the_los_lifecycle_boundary(self):
        for dataset, query in COST_SPIT_QUERIES.items():
            normalized = " ".join(query.lower().split())
            with self.subTest(dataset=dataset):
                self.assertIn("item.created_utc::date <= %(cutoff_date)s", normalized)
                self.assertIn(
                    "item.canceled_utc is null or item.canceled_utc::date > %(cutoff_date)s",
                    normalized,
                )
                # A final-state predicate would drop the exact records SPIT must
                # retain: bookings/items cancelled after the cutoff.
                self.assertNotIn(
                    "item.canceled_utc is null and",
                    normalized,
                )

    def test_reservation_derived_datasets_filter_both_lifecycle_levels(self):
        for dataset in (
            "roomRevenue",
            "arrivalsDepartures",
            "cleaningAllocations",
            "distributionMix",
        ):
            normalized = " ".join(COST_SPIT_QUERIES[dataset].lower().split())
            with self.subTest(dataset=dataset):
                self.assertIn(
                    "reservation.created_utc::date <= %(cutoff_date)s",
                    normalized,
                )
                self.assertIn(
                    "reservation.cancelled_utc is null or "
                    "reservation.cancelled_utc::date > %(cutoff_date)s",
                    normalized,
                )

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

        self.assertEqual(set(datasets), set(COST_SPIT_QUERIES))
        self.assertTrue(all(rows == [] for rows in datasets.values()))
        self.assertTrue(all(count == 0 for count in counts.values()))
        self.assertEqual(len(fake_pool.executions), len(COST_SPIT_QUERIES))
        for _query, parameters in fake_pool.executions:
            self.assertEqual(parameters["cutoff_date"], date(2025, 8, 19))


if __name__ == "__main__":
    unittest.main()
