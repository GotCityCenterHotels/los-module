"""Publication-time maintenance of the LOS read model.

The read path's covered range scan is only index-only where the visibility map
says the pages are all-visible, and a bulk publication leaves that map unset. So
the publication vacuums. Two properties matter and neither is obvious from
reading the call site: the vacuum must never be able to fail a publication that
has already committed, and it must not run inside the publication transaction.
"""

import os
import unittest

from unittest.mock import MagicMock, patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "integration_db")
os.environ.setdefault("DB_USER", "readonly")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from cost_database import cost_pool
from database import pool
from services import los_sync_service


# Matching the sibling test modules: the placeholder credentials above are never
# connectable, and leaving the pools open lets their reconnect threads outlive the
# interpreter and raise at finalization.
cost_pool.close()
pool.close()


class FakeConnection:
    def __init__(self, recorder):
        self.recorder = recorder
        self.autocommit = False
        self.cursor_obj = MagicMock()
        self.cursor_obj.execute.side_effect = self._execute

    def _execute(self, statement, *args):
        self.recorder.append((statement, self.autocommit))

    def cursor(self):
        cursor = MagicMock()
        cursor.__enter__ = lambda _self: self.cursor_obj
        cursor.__exit__ = lambda *_args: False
        return cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class VacuumTests(unittest.TestCase):
    def test_every_configured_table_is_vacuumed_and_analysed(self):
        recorded = []
        with patch.object(
            los_sync_service,
            "get_import_connection",
            side_effect=lambda: FakeConnection(recorded),
        ):
            los_sync_service._vacuum_read_model()

        statements = [statement for statement, _autocommit in recorded]
        self.assertEqual(len(statements), len(los_sync_service.VACUUM_TABLES))
        for table in los_sync_service.VACUUM_TABLES:
            self.assertIn(f"VACUUM (ANALYZE) {table}", statements)

    def test_the_read_table_is_among_them(self):
        # The one the interactive query actually scans.
        self.assertIn(
            "functions.reservation_los_daily", los_sync_service.VACUUM_TABLES
        )

    def test_the_vacuum_runs_in_autocommit(self):
        # VACUUM is refused inside a transaction block, so this is not a
        # preference - a non-autocommit connection makes the statement an error.
        recorded = []
        with patch.object(
            los_sync_service,
            "get_import_connection",
            side_effect=lambda: FakeConnection(recorded),
        ):
            los_sync_service._vacuum_read_model()

        for statement, autocommit in recorded:
            self.assertTrue(autocommit, f"{statement} ran without autocommit")

    def test_a_failing_vacuum_does_not_raise(self):
        """The publication is already committed by the time this runs.

        A blocked or refused vacuum is a lost optimisation. Letting it propagate
        would report a good publication as a failed sync and, on the timer path,
        retry the whole thing.
        """
        with patch.object(
            los_sync_service,
            "get_import_connection",
            side_effect=RuntimeError("lock not available"),
        ):
            los_sync_service._vacuum_read_model()  # must not raise

    def test_one_failing_table_does_not_stop_the_others(self):
        recorded = []
        calls = {"n": 0}

        def connect():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("busy")
            return FakeConnection(recorded)

        with patch.object(
            los_sync_service, "get_import_connection", side_effect=connect
        ):
            los_sync_service._vacuum_read_model()

        self.assertEqual(
            len(recorded), len(los_sync_service.VACUUM_TABLES) - 1
        )


class RetentionTests(unittest.TestCase):
    def test_only_one_superseded_publication_is_kept(self):
        """Eight generations of rows all sat in one index the read descends.

        The read filters on a single run_id, so the other seven were dead weight.
        One previous generation is what a rollback needs: the publication pointer
        moves back to it without re-running a sync.
        """
        self.assertEqual(los_sync_service.LOS_RUN_RETENTION, 1)

    def test_the_pruning_query_is_parameterised_by_the_retention(self):
        # A hardcoded OFFSET is what made this eight in the first place.
        import inspect

        source = inspect.getsource(los_sync_service)
        self.assertIn("OFFSET %s", source)
        self.assertIn("(run_id, LOS_RUN_RETENTION)", source)

    def test_retention_cannot_go_negative(self):
        # OFFSET rejects a negative, which would fail the publication itself.
        self.assertGreaterEqual(los_sync_service.LOS_RUN_RETENTION, 0)


if __name__ == "__main__":
    unittest.main()
