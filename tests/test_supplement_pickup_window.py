import os
import re
import unittest

from pathlib import Path


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from services import supplement_service

REPO_ROOT = Path(__file__).resolve().parent.parent


def point(days_before):
    return {"daysBeforeStay": days_before, "assignedRooms": days_before}


class PickupWindowTests(unittest.TestCase):
    """The lookback window slices a cached full-history curve.

    Slicing in Python rather than SQL is what keeps the query path free of a
    ceiling: the source query always returns the whole history, so a large
    request cannot be silently clipped.
    """

    def setUp(self):
        self.series = [point(days) for days in range(0, 400)]

    def test_none_keeps_the_entire_history(self):
        self.assertEqual(
            len(supplement_service._slice_pickup(self.series, None)),
            len(self.series),
        )

    def test_window_keeps_only_days_within_it(self):
        sliced = supplement_service._slice_pickup(self.series, 7)
        self.assertEqual([p["daysBeforeStay"] for p in sliced], list(range(0, 8)))

    def test_single_day_window_is_allowed(self):
        sliced = supplement_service._slice_pickup(self.series, 1)
        self.assertEqual([p["daysBeforeStay"] for p in sliced], [0, 1])

    def test_window_larger_than_history_is_not_an_error(self):
        # Asking for more than exists returns what exists, rather than failing
        # or padding the curve with empty days.
        sliced = supplement_service._slice_pickup(self.series, 10_000)
        self.assertEqual(len(sliced), len(self.series))

    def test_every_whole_day_is_reachable(self):
        # The control offers per-day granularity, so each step must change the
        # result by exactly one day.
        for days in range(1, 60):
            with self.subTest(days=days):
                self.assertEqual(
                    len(supplement_service._slice_pickup(self.series, days)),
                    days + 1,
                )

    def test_windowed_payload_reports_what_was_asked_and_what_exists(self):
        payload = {
            "pickup": self.series,
            "comparisonPickup": self.series[:100],
            "pickupHistoryDays": 399,
        }
        windowed = supplement_service._windowed_payload(payload, 30)
        self.assertEqual(windowed["daysBeforeStay"], 30)
        self.assertEqual(windowed["pickupHistoryDays"], 399)
        self.assertEqual(len(windowed["pickup"]), 31)
        self.assertEqual(len(windowed["comparisonPickup"]), 31)

    def test_windowed_payload_leaves_the_cached_series_intact(self):
        # The cache holds the full curve; a window must not mutate it, or the
        # next request would see a shortened history.
        payload = {
            "pickup": self.series,
            "comparisonPickup": [],
            "pickupHistoryDays": 399,
        }
        supplement_service._windowed_payload(payload, 5)
        self.assertEqual(len(payload["pickup"]), 400)


class PickupQueryPathTests(unittest.TestCase):
    def test_no_366_day_cap_remains_in_the_pickup_path(self):
        # The old query clipped at "snapshot_date BETWEEN stay_date - 366 AND
        # stay_date + 7", which silently truncated any longer window.
        source = (REPO_ROOT / "services" / "supplement_service.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("- 366) AND", source)

    def test_history_is_rebuilt_from_lifecycle_not_stored_snapshots(self):
        source = (REPO_ROOT / "services" / "supplement_service.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("fetch_pickup_history", source)

    def test_reconstruction_matches_the_sync_eligibility_rule(self):
        # A reconstructed point must equal the stored point for any date both
        # cover, so the predicates have to stay identical to the ones
        # _materialize_snapshot_facts uses.
        pickup = (REPO_ROOT / "queries" / "supplement_source.py").read_text(
            encoding="utf-8"
        )
        sync = (REPO_ROOT / "services" / "supplement_sync_service.py").read_text(
            encoding="utf-8"
        )
        for predicate in (
            "reservation_created_date <=",
            "reservation_cancelled_date >",
            "item_created_date <=",
            "item_cancelled_date >",
        ):
            with self.subTest(predicate=predicate):
                self.assertIn(predicate, pickup)
                self.assertIn(predicate, sync)

    def test_reconstruction_starts_at_the_first_booking(self):
        pickup = (REPO_ROOT / "queries" / "supplement_source.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("first_booking_date", pickup)
        self.assertRegex(pickup, r"least\(\s*min\(reservation_created_date\)")

    def test_reconstruction_reads_only(self):
        # integration_db must never be written to.
        pickup = (REPO_ROOT / "queries" / "supplement_source.py").read_text(
            encoding="utf-8"
        )
        statement = re.search(
            r'PICKUP_HISTORY_SQL = """(.*?)"""', pickup, re.S
        ).group(1)
        # Word boundaries matter: column names like created_utc and
        # cancelled_utc contain these keywords as substrings.
        for forbidden in ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"):
            with self.subTest(keyword=forbidden):
                self.assertIsNone(
                    re.search(rf"\b{forbidden}\b", statement, re.IGNORECASE),
                    f"{forbidden} must not appear in a read-only source query",
                )


if __name__ == "__main__":
    unittest.main()
