import os
import threading
import time
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

from services import hotels_service


hotels_service.pool.close()


class FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, parameters):
        with self.owner.lock:
            self.owner.execution_count += 1
        self.owner.started.set()
        self.owner.release.wait(timeout=2)

    def fetchall(self):
        return [("A",), ("B",)]


class FakeConnection:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return FakeCursor(self.owner)


class FakePool:
    def __init__(self):
        self.execution_count = 0
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()

    def connection(self):
        return FakeConnection(self)


class HotelServiceTests(unittest.TestCase):
    def setUp(self):
        self.original_pool = hotels_service.pool
        self.fake_pool = FakePool()
        hotels_service.pool = self.fake_pool
        hotels_service._cache.clear()
        hotels_service._inflight.clear()

    def tearDown(self):
        hotels_service.pool = self.original_pool
        hotels_service._cache.clear()
        hotels_service._inflight.clear()

    def test_concurrent_identical_misses_share_one_query(self):
        arguments = (date(2026, 1, 1), date(2026, 12, 31), "sameDate")
        results = []
        errors = []

        def invoke():
            try:
                results.append(hotels_service.fetch_hotels(*arguments))
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=invoke)
        second = threading.Thread(target=invoke)
        first.start()
        self.assertTrue(self.fake_pool.started.wait(timeout=1))
        second.start()
        time.sleep(0.05)
        self.fake_pool.release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(results, [["A", "B"], ["A", "B"]])
        self.assertEqual(self.fake_pool.execution_count, 1)


if __name__ == "__main__":
    unittest.main()
