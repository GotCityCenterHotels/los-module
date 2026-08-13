import os
import unittest

from datetime import date
from unittest.mock import patch


os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

import backfill_supplement


class SupplementBackfillRangeTests(unittest.TestCase):
    def test_range_runs_one_snapshot_at_a_time_in_date_order(self):
        called = []

        def run(snapshot_date):
            called.append(snapshot_date)
            return {
                "runId": len(called),
                "exportedRows": 10,
                "importedRows": 4,
            }

        output = []
        with patch.object(backfill_supplement, "run_backfill_partition", side_effect=run):
            result = backfill_supplement.run_backfill_range(
                date(2026, 8, 11), date(2026, 8, 13), output=output.append
            )

        self.assertEqual(
            called,
            [date(2026, 8, 11), date(2026, 8, 12), date(2026, 8, 13)],
        )
        self.assertEqual(result["completedDates"], 3)
        self.assertEqual(result["exportedRows"], 30)
        self.assertEqual(result["importedRows"], 12)

    def test_single_date_remains_supported(self):
        with patch.object(
            backfill_supplement,
            "run_backfill_partition",
            return_value={"runId": 1, "exportedRows": 2, "importedRows": 1},
        ) as run:
            result = backfill_supplement.run_backfill_range(
                date(2026, 8, 13), date(2026, 8, 13), output=lambda *_args, **_kwargs: None
            )

        run.assert_called_once_with(date(2026, 8, 13))
        self.assertEqual(result["completedDates"], 1)

    def test_invalid_reverse_range_is_rejected_before_source_access(self):
        with patch.object(backfill_supplement, "run_backfill_partition") as run:
            with self.assertRaisesRegex(ValueError, "before"):
                backfill_supplement.run_backfill_range(
                    date(2026, 8, 14), date(2026, 8, 13)
                )
        run.assert_not_called()

    def test_failure_stops_before_later_dates(self):
        called = []

        def run(snapshot_date):
            called.append(snapshot_date)
            if snapshot_date == date(2026, 8, 12):
                raise RuntimeError("source timeout")
            return {"runId": 1, "exportedRows": 2, "importedRows": 1}

        with patch.object(backfill_supplement, "run_backfill_partition", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "source timeout"):
                backfill_supplement.run_backfill_range(
                    date(2026, 8, 11),
                    date(2026, 8, 13),
                    output=lambda *_args, **_kwargs: None,
                )

        self.assertEqual(called, [date(2026, 8, 11), date(2026, 8, 12)])


if __name__ == "__main__":
    unittest.main()
