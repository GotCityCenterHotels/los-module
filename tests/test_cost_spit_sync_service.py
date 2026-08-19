import os
import unittest

from datetime import date
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from services import cost_spit_sync_service as sync


class CursorContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class SetupCursor(CursorContext):
    def __init__(self):
        self.executions = []

    def execute(self, query, parameters=None):
        self.executions.append((query, parameters))


class SourceCursor(CursorContext):
    def __init__(self, rows):
        self.rows = list(rows)
        self.executions = []
        self.itersize = None

    def execute(self, query, parameters=None):
        self.executions.append((query, parameters))

    def fetchmany(self, size):
        batch, self.rows = self.rows[:size], self.rows[size:]
        return batch


class SourceConnection(CursorContext):
    def __init__(self, rows):
        self.setup = SetupCursor()
        self.source = SourceCursor(rows)

    def cursor(self, name=None):
        return self.source if name else self.setup


class TargetCursor(CursorContext):
    def __init__(self):
        self.batches = []

    def executemany(self, query, rows):
        self.batches.append((query, list(rows)))


class TargetConnection(CursorContext):
    def __init__(self):
        self.target = TargetCursor()
        self.commits = 0

    def cursor(self):
        return self.target

    def commit(self):
        self.commits += 1


class CostSpitSyncTests(unittest.TestCase):
    def test_migration_uses_immutable_runs_and_an_indexed_daily_key(self):
        migration = (
            Path(__file__).resolve().parent.parent
            / "sql"
            / "migrations"
            / "020_cost_spit_read_model.sql"
        ).read_text(encoding="utf-8").lower()

        self.assertIn("functions.cost_spit_sync_runs", migration)
        self.assertIn("functions.cost_spit_publication", migration)
        self.assertIn(
            "primary key (run_id, comparison_basis, stay_date, dataset)",
            " ".join(migration.split()),
        )
        self.assertIn("on delete cascade", migration)
        self.assertNotIn("integration_db", migration)

    def test_snapshot_plan_matches_the_http_comparison_dates(self):
        plan = sync.snapshot_plan(date(2026, 8, 19))

        self.assertEqual(plan["sameDate"], {
            "cutoff_date": date(2025, 8, 19),
            "start_date": date(2025, 1, 1),
            "end_date": date(2025, 12, 31),
        })
        self.assertEqual(plan["sameWeekday"], {
            "cutoff_date": date(2025, 8, 20),
            "start_date": date(2025, 1, 2),
            "end_date": date(2026, 1, 1),
        })

    def test_source_rows_are_compacted_to_one_array_per_dataset_day(self):
        source = SourceConnection([
            {
                "dataset": "arrivalsDepartures",
                "payload": {"stay_date": "2025-01-01", "hotel_name": "A"},
            },
            {
                "dataset": "arrivalsDepartures",
                "payload": {"stay_date": "2025-01-01", "hotel_name": "B"},
            },
            {
                "dataset": "breakfast",
                "payload": {"stay_date": "2025-01-02", "hotel_name": "A"},
            },
        ])
        target = TargetConnection()
        plan = {
            "cutoff_date": date(2025, 8, 19),
            "start_date": date(2025, 1, 1),
            "end_date": date(2025, 12, 31),
        }

        with patch.object(sync, "get_export_connection", return_value=source), \
             patch.object(sync, "get_import_connection", return_value=target):
            exported, imported = sync._stream_basis(12, "sameDate", plan)

        self.assertEqual((exported, imported), (3, 2))
        self.assertEqual(target.commits, 1)
        written = target.target.batches[0][1]
        self.assertEqual(
            [(row[2], row[3], len(row[4].obj)) for row in written],
            [
                (date(2025, 1, 1), "arrivalsDepartures", 2),
                (date(2025, 1, 2), "breakfast", 1),
            ],
        )
        self.assertIn("SET LOCAL work_mem", source.setup.executions[0][0])
        self.assertEqual(
            source.source.executions[0][1],
            {
                "start_date": date(2025, 1, 1),
                "end_date": date(2025, 12, 31),
                "cutoff_date": date(2025, 8, 19),
            },
        )

    def test_a_row_outside_the_published_coverage_is_rejected(self):
        source = SourceConnection([{
            "dataset": "payments",
            "payload": {"stay_date": "2024-12-31", "hotel_name": "A"},
        }])
        target = TargetConnection()
        plan = {
            "cutoff_date": date(2025, 8, 19),
            "start_date": date(2025, 1, 1),
            "end_date": date(2025, 12, 31),
        }

        with patch.object(sync, "get_export_connection", return_value=source), \
             patch.object(sync, "get_import_connection", return_value=target), \
             self.assertRaisesRegex(RuntimeError, "outside coverage"):
            sync._stream_basis(13, "sameDate", plan)


if __name__ == "__main__":
    unittest.main()
