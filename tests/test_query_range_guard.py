import os
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

import function_app
from cost_database import cost_pool
from database import pool


cost_pool.close()
pool.close()


class QueryRangeGuardTests(unittest.TestCase):
    """Static Web Apps aborts a linked-backend call at ~45s, so an unbounded
    range is not merely slow - it is a guaranteed "Backend call failure"."""

    def test_range_within_the_cap_is_accepted(self):
        self.assertIsNone(
            function_app.validate_range_span(date(2026, 1, 1), date(2026, 12, 31))
        )

    def test_range_exactly_at_the_cap_is_accepted(self):
        start = date(2026, 1, 1)
        end = start.fromordinal(start.toordinal() + function_app.MAX_RANGE_DAYS - 1)
        self.assertIsNone(function_app.validate_range_span(start, end))

    def test_range_one_day_past_the_cap_is_rejected(self):
        start = date(2026, 1, 1)
        end = start.fromordinal(start.toordinal() + function_app.MAX_RANGE_DAYS)
        response = function_app.validate_range_span(start, end)
        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 400)

    def test_decade_wide_range_is_rejected_rather_than_timing_out(self):
        response = function_app.validate_range_span(date(2000, 1, 1), date(2030, 12, 31))
        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 400)

    def test_single_day_range_is_accepted(self):
        self.assertIsNone(
            function_app.validate_range_span(date(2026, 6, 1), date(2026, 6, 1))
        )


if __name__ == "__main__":
    unittest.main()
