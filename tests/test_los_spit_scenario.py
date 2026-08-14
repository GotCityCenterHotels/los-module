import os
import unittest


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")


def spit_bookings(slices, cutoff, created_date):
    """Reference model of the SPIT rule, mirroring the spit_reservation CTE.

    NOT an integration test: this does not execute the SQL. It pins the agreed
    definition so a future edit to either implementation can be checked against
    it, and documents why counting stored rows was wrong.

    Both implementations must satisfy: collapse a reservation's stored rows to
    one row (summing length of stay) BEFORE counting bookings.
    """
    if created_date > cutoff:
        return []
    alive = [
        stay_slice for stay_slice in slices
        if stay_slice["cancelled"] is None or stay_slice["cancelled"] > cutoff
    ]
    if not alive:
        return []
    return [sum(stay_slice["los"] for stay_slice in alive)]


CUTOFF = "2025-08-14"
CREATED = "2024-01-15"


class SpitScenarioRuleTests(unittest.TestCase):
    def test_partial_cancellation_after_cutoff_stays_one_booking(self):
        # The reported bug: a 3-night stay shortened to 2 nights after the
        # cutoff was stored as two rows and counted as two bookings, splitting
        # its length of stay into a 2-night and a 1-night booking.
        stay = [
            {"cancelled": None, "los": 2},
            {"cancelled": "2025-09-10", "los": 1},
        ]
        self.assertEqual(spit_bookings(stay, CUTOFF, CREATED), [3])

    def test_multiple_cancellation_dates_still_one_booking(self):
        stay = [
            {"cancelled": None, "los": 1},
            {"cancelled": "2025-09-10", "los": 1},
            {"cancelled": "2025-10-02", "los": 2},
        ]
        self.assertEqual(spit_bookings(stay, CUTOFF, CREATED), [4])

    def test_cancellation_before_cutoff_is_excluded_from_the_stay(self):
        # Those nights were already gone at the cutoff, so they must not count.
        stay = [
            {"cancelled": None, "los": 2},
            {"cancelled": "2025-03-01", "los": 1},
        ]
        self.assertEqual(spit_bookings(stay, CUTOFF, CREATED), [2])

    def test_fully_cancelled_before_cutoff_is_not_a_booking(self):
        stay = [{"cancelled": "2025-03-01", "los": 3}]
        self.assertEqual(spit_bookings(stay, CUTOFF, CREATED), [])

    def test_fully_cancelled_after_cutoff_still_counts_at_full_length(self):
        stay = [{"cancelled": "2025-09-10", "los": 3}]
        self.assertEqual(spit_bookings(stay, CUTOFF, CREATED), [3])

    def test_reservation_created_after_cutoff_is_excluded(self):
        stay = [{"cancelled": None, "los": 3}]
        self.assertEqual(spit_bookings(stay, CUTOFF, "2026-01-01"), [])

    def test_untouched_reservation_is_unaffected(self):
        # The common case must not change: no cancellations, one booking.
        stay = [{"cancelled": None, "los": 4}]
        self.assertEqual(spit_bookings(stay, CUTOFF, CREATED), [4])

    def test_room_nights_were_already_correct_and_stay_correct(self):
        # Counting rows produced the right night total but the wrong booking
        # count and distribution. The fix must preserve the night total.
        stay = [
            {"cancelled": None, "los": 2},
            {"cancelled": "2025-09-10", "los": 1},
        ]
        old_rows = [s["los"] for s in stay]  # what the old query counted
        new_rows = spit_bookings(stay, CUTOFF, CREATED)
        self.assertEqual(sum(old_rows), sum(new_rows))
        self.assertEqual(len(old_rows), 2)
        self.assertEqual(len(new_rows), 1)


class AggregateSqlShapeTests(unittest.TestCase):
    def test_both_implementations_collapse_by_reservation_before_counting(self):
        from queries import los_facts
        from services import los_sync_service

        for label, sql in (
            ("read model", los_sync_service.AGGREGATE_SQL),
            ("direct query", los_facts.LOS_FACTS_SQL),
        ):
            with self.subTest(implementation=label):
                self.assertIn("spit_reservation", sql)
                # sum(los) is what collapses a reservation's stored rows back
                # into a single booking of the right length.
                self.assertIn("sum(", sql)
                # The old shape counted stored rows directly into a scenario.
                self.assertNotIn("spit_booking_count", sql)


if __name__ == "__main__":
    unittest.main()
