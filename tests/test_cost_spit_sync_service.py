import inspect
import json
import os
import unittest

from datetime import date, timedelta
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

    def test_the_shape_migration_stores_response_ready_json(self):
        """json rather than jsonb, because json keeps the text verbatim: the
        publisher writes the exact bytes the response sends and the read copies
        them out again without either side reserialising."""
        migration = (
            Path(__file__).resolve().parent.parent
            / "sql"
            / "migrations"
            / "021_cost_spit_read_model_shape.sql"
        ).read_text(encoding="utf-8").lower()
        ddl = "\n".join(
            line for line in migration.splitlines()
            if not line.strip().startswith("--")
        )

        self.assertIn("fact_rows json not null", ddl)
        self.assertIn("json_typeof(fact_rows) = 'array'", ddl)
        self.assertNotIn("jsonb", ddl)
        self.assertIn("fact_count integer not null", ddl)

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

    def test_coverage_runs_to_year_end_not_to_today(self):
        """The regression that made SPIT look empty for every future date.

        The cutoff bounds when a booking was made, not which nights it covers.
        Last August the book already held reservations for the coming December,
        and those are exactly the rows a forward-looking comparison needs. A
        window that stopped at today answered the past and returned nothing for
        the future, which on screen is indistinguishable from missing data.
        """
        for as_of in (date(2026, 1, 5), date(2026, 8, 20), date(2026, 11, 30)):
            start, end = sync.coverage_window(as_of)
            self.assertEqual(start, date(as_of.year, 1, 1))
            self.assertEqual(end, date(as_of.year, 12, 31))
            self.assertGreater(
                end, as_of,
                f"coverage on {as_of} has to reach past today",
            )

    def test_a_stay_date_after_today_is_inside_published_coverage(self):
        """The reading the bug actually broke: December, asked in August.

        Walked across the year for both bases, because the route and the
        publisher shift dates in different places and only have to disagree on
        one day for a column to go blank.
        """
        from shared.comparison_dates import shift_cost_comparison_date

        for offset in range(0, 366):
            today = date(2026, 1, 1) + timedelta(days=offset)
            plan = sync.snapshot_plan(today)

            # The page's full-year range: every night of this year, which is
            # mostly nights that have not happened yet.
            for requested in (date(today.year, 1, 1),
                              today,
                              date(today.year, 12, 31)):
                for basis in sync.COMPARISON_BASES:
                    asked = shift_cost_comparison_date(requested, basis)
                    published = plan[basis]
                    self.assertGreaterEqual(
                        asked, published["start_date"],
                        f"{basis} on {today}: {requested} precedes coverage",
                    )
                    self.assertLessEqual(
                        asked, published["end_date"],
                        f"{basis} on {today}: {requested} is past coverage",
                    )

    def test_a_stale_publication_still_covers_the_whole_year(self):
        """Coverage no longer moves with today, so a publication a few nights
        old covers exactly what a fresh one does. That is what makes the
        staleness allowance safe rather than a source of blank columns."""
        from shared.comparison_dates import shift_cost_comparison_date
        from services.cost_data_service import COST_SPIT_MAX_STALE_DAYS

        today = date(2026, 8, 20)
        stale = sync.snapshot_plan(
            today - timedelta(days=COST_SPIT_MAX_STALE_DAYS)
        )
        for basis in sync.COMPARISON_BASES:
            self.assertLessEqual(
                shift_cost_comparison_date(date(today.year, 12, 31), basis),
                stale[basis]["end_date"],
            )

    def test_the_two_bases_are_streamed_concurrently(self):
        """Two independent source queries on their own connections writing
        disjoint rows. Run one after the other, their durations simply added."""
        source = inspect.getsource(sync.sync_cost_spit)
        self.assertIn("ThreadPoolExecutor", source)
        self.assertIn("_stream_basis, run_id, basis", source)

    def test_the_source_sort_avoids_reparsing_the_payload(self):
        """The final sort covers every fact row in the window. Ordering it by
        payload ->> 'stay_date' made Postgres pull two text keys out of a jsonb
        value per row before it could compare anything."""
        from queries.cost_spit import COST_SPIT_SQL

        statements = " ".join(
            line for line in COST_SPIT_SQL.lower().splitlines()
            if not line.strip().startswith("--")
        )
        normalized = " ".join(statements.split())
        self.assertIn(
            "order by dataset_order, stay_date, hotel_name", normalized
        )
        self.assertNotIn("payload ->> 'stay_date'", normalized)

    def test_source_rows_are_compacted_to_one_array_per_dataset_day(self):
        source = SourceConnection([
            {
                "dataset": "arrivalsDepartures",
                "stay_date": date(2025, 1, 1),
                "payload": {
                    "stay_date": "2025-01-01",
                    "hotel_name": "A",
                    "last_updated_at": None,
                },
            },
            {
                "dataset": "arrivalsDepartures",
                "stay_date": date(2025, 1, 1),
                "payload": {
                    "stay_date": "2025-01-01",
                    "hotel_name": "B",
                    "last_updated_at": None,
                },
            },
            {
                "dataset": "breakfast",
                "stay_date": date(2025, 1, 2),
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
            [(row[2], row[3], row[4]) for row in written],
            [
                (date(2025, 1, 1), "arrivalsDepartures", 2),
                (date(2025, 1, 2), "breakfast", 1),
            ],
        )
        # Stored in the shape the response sends: camelCase keys, no nulls, and
        # already serialized, so an HTTP read neither renames nor re-encodes.
        self.assertEqual(json.loads(written[0][5]), [
            {"stayDate": "2025-01-01", "hotelName": "A"},
            {"stayDate": "2025-01-01", "hotelName": "B"},
        ])
        self.assertNotIn(" ", written[0][5])
        self.assertIn("%s::json", sync.INSERT_DAILY_SQL)
        self.assertIn("SET LOCAL work_mem", source.setup.executions[0][0])
        self.assertEqual(
            source.source.executions[0][1],
            {
                "start_date": date(2025, 1, 1),
                "end_date": date(2025, 12, 31),
                "cutoff_date": date(2025, 8, 19),
            },
        )

    def test_an_abandoned_run_is_discarded_before_a_new_one_starts(self):
        """A run killed mid-stream keeps its committed daily rows behind a row
        that still says running, which retention skips because it only prunes
        finished runs. Holding the advisory lock means nothing else is live."""
        source = inspect.getsource(sync._create_run)
        self.assertIn("status = 'running'", source)
        self.assertIn("make_interval", source)
        self.assertIn("ABANDONED_RUN_HOURS", source)

    def test_the_source_read_gets_a_background_statement_ceiling(self):
        """The five minute export default is the short-statement ceiling. This
        is the lifecycle scan the whole read model exists to move off the
        request path, and it is allowed to take longer than that."""
        self.assertGreater(sync.SOURCE_STATEMENT_TIMEOUT_MS, 300000)
        self.assertIn(
            "get_export_connection(SOURCE_STATEMENT_TIMEOUT_MS)",
            inspect.getsource(sync._stream_basis),
        )

    def test_the_build_lock_does_not_hold_a_pooled_connection(self):
        """The lock is held for the whole build. The pool is four wide with the
        Cost Data read already capped at three, so borrowing one here left
        nothing for the lookups every page request makes before anything else."""
        source = inspect.getsource(sync.sync_cost_spit)
        self.assertIn("get_import_connection() as lock_connection", source)
        self.assertNotIn("cost_pool.connection() as lock_connection", source)

    def test_a_row_outside_the_published_coverage_is_rejected(self):
        source = SourceConnection([{
            "dataset": "payments",
            "stay_date": date(2024, 12, 31),
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
