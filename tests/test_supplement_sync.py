import os
import unittest

from contextlib import nullcontext
from datetime import date
from unittest.mock import patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "integration_db")
os.environ.setdefault("DB_USER", "readonly")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from services import supplement_sync_service as sync_service


class FakeCursor:
    def __init__(self, events, published=date(2026, 8, 12), acquired=True):
        self.events = events
        self.published = published
        self.acquired = acquired
        self.current = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, parameters=None):
        normalized = " ".join(str(query).split())
        self.events.append(("execute", normalized, parameters))
        if "pg_try_advisory_lock" in normalized:
            self.current = {"acquired": self.acquired}
        elif "SELECT data_as_of" in normalized:
            self.current = {"data_as_of": self.published} if self.published else None
        elif "INSERT INTO functions.supplement_sync_runs" in normalized:
            self.current = {"run_id": 41}
        elif "INSERT INTO functions.cost_publication" in normalized:
            self.current = {"version": 12}
        else:
            self.current = None

    def fetchone(self):
        return self.current

    def executemany(self, query, parameters):
        self.events.append(("executemany", query, parameters))


class FakeConnection:
    def __init__(self, published=date(2026, 8, 12), acquired=True):
        self.events = []
        self.cursor_instance = FakeCursor(self.events, published, acquired)

    def cursor(self, **_kwargs):
        return self.cursor_instance

    def commit(self):
        self.events.append(("commit",))

    def rollback(self):
        self.events.append(("rollback",))


class SupplementCoverageSourceTests(unittest.TestCase):
    """Coverage must describe the table the grid reads.

    The stay-date window is what fetch_supplement_grid clips against, and the
    grid's own figures come from supplement_latest_inventory - which
    _apply_retention explicitly never deletes from. Deriving the window from
    supplement_snapshot_inventory instead made the grid decline dates it held
    perfectly good facts for: those snapshot tables are pruned with
    `snapshot_date > stay_date + 7`, so a stay date more than a week old keeps its
    permanent facts and loses its snapshots, and the window collapsed to roughly
    `min(snapshot_date) - 7`. That is why 1 August returned nothing on 20 August.
    """

    def _coverage_statement(self):
        events = []
        cursor = FakeCursor(events)
        sync_service._refresh_coverage(cursor)
        statements = [
            query for kind, query, _params in events
            if kind == "execute"
            and "INSERT INTO functions.supplement_coverage" in query
        ]
        self.assertEqual(len(statements), 1)
        return statements[0]

    def test_the_stay_window_comes_from_the_permanent_latest_table(self):
        statement = self._coverage_statement()

        stay_half = statement[:statement.index("CROSS JOIN")]
        self.assertIn("min(stay_date) AS minimum_stay_date", stay_half)
        self.assertIn("functions.supplement_latest_inventory", stay_half)
        # The pruned table must not be what bounds the stay-date window.
        self.assertNotIn("supplement_snapshot_inventory", stay_half)

    def test_the_snapshot_window_still_comes_from_the_snapshot_table(self):
        statement = self._coverage_statement()

        snapshot_half = statement[statement.index("CROSS JOIN"):]
        self.assertIn("min(snapshot_date) AS minimum_snapshot_date", snapshot_half)
        self.assertIn("functions.supplement_snapshot_inventory", snapshot_half)

    def test_the_write_is_skipped_rather_than_failing_on_empty_tables(self):
        # Every date column is NOT NULL and min() over an empty table is NULL, so
        # without this guard a first run or a fully-pruned snapshot table would
        # abort the publication transaction over bookkeeping.
        statement = self._coverage_statement()

        self.assertIn("WHERE stay.minimum_stay_date IS NOT NULL", statement)
        self.assertIn("AND snap.minimum_snapshot_date IS NOT NULL", statement)

    def test_retention_reuses_the_same_coverage_writer(self):
        # Two call sites wrote this statement independently, so they could drift
        # and one could keep reading the wrong table.
        events = []
        cursor = FakeCursor(events)
        sync_service._apply_retention(cursor, date(2026, 8, 20))
        coverage = [
            query for kind, query, _params in events
            if kind == "execute"
            and "INSERT INTO functions.supplement_coverage" in query
        ]
        self.assertEqual(len(coverage), 1)
        self.assertIn("functions.supplement_latest_inventory", coverage[0])
        # And the column list still matches the six values selected.
        self.assertIn(
            "singleton, minimum_stay_date, maximum_stay_date, "
            "minimum_snapshot_date, maximum_snapshot_date, updated_at",
            coverage[0],
        )


class SupplementSyncOrchestrationTests(unittest.TestCase):
    def test_inventory_variance_is_compared_per_snapshot_not_batch_total(self):
        self.assertFalse(sync_service._inventory_variance_exceeds(52, 52))
        self.assertFalse(sync_service._inventory_variance_exceeds(0, 208))
        self.assertTrue(sync_service._inventory_variance_exceeds(52, 208))

    def _patches(self, connection, validate_side_effect=None):
        validate = patch.object(
            sync_service,
            "_validate_stages",
            return_value=(12, 4),
            side_effect=validate_side_effect,
        )
        return (
            patch.object(sync_service, "ensure_supplement_schema"),
            patch.object(sync_service.cost_pool, "connection", return_value=nullcontext(connection)),
            patch.object(sync_service, "stockholm_today", return_value=date(2026, 8, 13)),
            patch.object(sync_service, "iter_booking_lifecycle_batches", return_value=iter(())),
            patch.object(sync_service, "iter_inventory_batches", return_value=iter(())),
            patch.object(sync_service, "_create_stages"),
            patch.object(sync_service, "_materialize_snapshot_facts"),
            validate,
        )

    def test_delta_rereads_latest_and_preceding_three_dates_then_publishes_atomically(self):
        connection = FakeConnection()
        snapshots = [date(2026, 8, day) for day in range(10, 14)]
        patches = self._patches(connection)
        with patches[0], patches[1], patches[2], patches[3] as booking, \
                patches[4] as inventory, patches[5], patches[6], patches[7], patch.object(
                    sync_service, "_publish_stage",
                    side_effect=lambda *_args: connection.events.append(("publish-stage",)),
                ) as publish:
            result = sync_service.sync_supplement("delta")

        self.assertEqual(result["status"], "published")
        self.assertEqual(booking.call_count, 1)
        self.assertEqual(booking.call_args.args[0], snapshots)
        inventory.assert_called_once_with(snapshots, 5000)
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[2], snapshots)
        event_names = [event[0] for event in connection.events]
        publication_index = next(
            index for index, event in enumerate(connection.events)
            if event[0] == "execute"
            and "INSERT INTO functions.supplement_publication" in event[1]
        )
        self.assertLess(event_names.index("publish-stage"), publication_index)
        self.assertNotIn("rollback", event_names)

    def test_validation_failure_rolls_back_and_does_not_move_publication(self):
        connection = FakeConnection()
        patches = self._patches(connection, validate_side_effect=ValueError("bad source"))
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patch.object(sync_service, "_publish_stage"):
            with self.assertRaisesRegex(ValueError, "bad source"):
                sync_service.sync_supplement("delta")

        event_names = [event[0] for event in connection.events]
        self.assertIn("rollback", event_names)
        sql = "\n".join(event[1] for event in connection.events if event[0] == "execute")
        self.assertNotIn("INSERT INTO functions.supplement_publication", sql)
        self.assertIn("SET status = 'failed'", sql)

    def test_the_mews_category_ordering_reaches_the_room_category_mirror(self):
        # Space categories are listed in the Mews ordering everywhere in the
        # app, so sort_order has to carry it - it used to be hardcoded to 0,
        # which left every list alphabetical.
        class StageCursor(FakeCursor):
            def execute(self, query, parameters=None):
                super().execute(query, parameters)
                if "min(stay_date)" in " ".join(str(query).split()):
                    self.current = {
                        "minimum_stay_date": date(2026, 8, 6),
                        "maximum_stay_date": date(2028, 2, 13),
                    }

        events = []
        sync_service._publish_stage(StageCursor(events), 41, [date(2026, 8, 13)])

        upsert = next(
            event[1] for event in events
            if event[0] == "execute"
            and "INSERT INTO functions.supplement_room_categories" in event[1]
        )
        self.assertIn("source.category_ordering, now()", upsert)
        self.assertIn("sort_order = EXCLUDED.sort_order", upsert)

    def test_the_inventory_stage_carries_the_category_ordering(self):
        row = {
            "snapshot_date": date(2026, 8, 13), "tenant_key": "GCCH",
            "enterprise_id": "p-1", "hotel_name": " Hotel A ",
            "category_id": "cat-1", "category_name": " Double ",
            "category_ordering": 3, "physical_inventory": 10,
            "sellable_inventory": 9, "inventory_quality": "exact",
        }
        self.assertEqual(sync_service._inventory_stage_row(row)[6], 3)

        # A mirror with no ordering column sends nothing; the category must
        # sort after the ordered ones rather than jumping to the front.
        del row["category_ordering"]
        self.assertEqual(
            sync_service._inventory_stage_row(row)[6],
            sync_service.UNORDERED_CATEGORY_RANK,
        )

    def test_advisory_lock_blocks_overlapping_run_before_source_access(self):
        connection = FakeConnection(acquired=False)
        with patch.object(sync_service, "ensure_supplement_schema"), patch.object(
            sync_service.cost_pool,
            "connection",
            return_value=nullcontext(connection),
        ), patch.object(sync_service, "stockholm_today") as source:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                sync_service.sync_supplement("delta")
        source.assert_not_called()


if __name__ == "__main__":
    unittest.main()
