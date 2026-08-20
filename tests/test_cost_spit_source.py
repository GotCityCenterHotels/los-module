import json
import os
import threading
import unittest

from datetime import date, timedelta


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from queries.cost_spit import COST_SPIT_DATASETS, COST_SPIT_READ_SQL, COST_SPIT_SQL
from services import cost_data_service


class FakeCursor:
    def __init__(self, executions, lock, rows):
        self.executions = executions
        self.lock = lock
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters):
        with self.lock:
            self.executions.append((query, parameters))

    def fetchall(self):
        return self.rows


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
    def __init__(self, rows=None):
        self.executions = []
        self.lock = threading.Lock()
        self.max_size = 4
        self.rows = rows or []

    def connection(self):
        return FakeConnection(FakeCursor(self.executions, self.lock, self.rows))


def publication_row(**overrides):
    row = {
        "run_id": 4,
        "cutoff_date": date(2025, 8, 19),
        "minimum_stay_date": date(2025, 1, 1),
        "maximum_stay_date": date(2025, 12, 31),
        "dataset": None,
        "stay_date": None,
        "fact_count": 0,
        "fact_rows": None,
    }
    row.update(overrides)
    return row


class CostSpitSourceTests(unittest.TestCase):
    def setUp(self):
        cost_data_service._result_cache.clear()
        cost_data_service._result_inflight.clear()

    def tearDown(self):
        cost_data_service._result_cache.clear()
        cost_data_service._result_inflight.clear()

    def test_the_consolidated_read_uses_the_los_lifecycle_boundary(self):
        normalized = " ".join(COST_SPIT_SQL.lower().split())
        self.assertIn(
            "item.created_utc < (%(cutoff_date)s::date + 1)::timestamptz",
            normalized,
        )
        self.assertIn(
            "item.canceled_utc is null or item.canceled_utc >= "
            "(%(cutoff_date)s::date + 1)::timestamptz",
            normalized,
        )
        # A final-state predicate would drop the exact records SPIT must retain:
        # bookings/items cancelled after the cutoff.
        self.assertNotIn("item.canceled_utc is null and", normalized)

    def test_the_lifecycle_boundary_stays_off_the_indexed_columns(self):
        """order_item_current is 5 GB with a btree on created_utc. Casting the
        column to date selects the same rows and hides them from that index, so
        the boundary is written as a range on the raw timestamp."""
        normalized = " ".join(COST_SPIT_SQL.lower().split())
        self.assertNotIn("created_utc::date", normalized)
        self.assertNotIn("canceled_utc::date", normalized)
        self.assertNotIn("cancelled_utc::date", normalized)

    def test_reservation_derived_facts_filter_both_lifecycle_levels(self):
        normalized = " ".join(COST_SPIT_SQL.lower().split())
        self.assertIn(
            "reservation_created_utc < (%(cutoff_date)s::date + 1)::timestamptz",
            normalized,
        )
        self.assertIn(
            "reservation_cancelled_utc is null or reservation_cancelled_utc >= "
            "(%(cutoff_date)s::date + 1)::timestamptz",
            normalized,
        )

    def test_the_mix_joins_name_columns_the_mirror_actually_has(self):
        """staging.travel_agency carries travel_agency_id/travel_agency_name and
        a rate's name is rate_current.name. The previous guess named agency.id,
        agency.name and rate.rate_name, so the query failed to parse and this
        dataset had never once been built."""
        normalized = " ".join(COST_SPIT_SQL.lower().split())
        self.assertIn(
            "agency.travel_agency_id = reservation.travel_agency_id", normalized
        )
        self.assertIn("rate.id = reservation.rate_id", normalized)
        self.assertNotIn("agency.id::text", normalized)
        self.assertNotIn("rate.rate_name", normalized)

    def test_the_full_range_scans_items_twice_not_once_per_dataset(self):
        normalized = " ".join(COST_SPIT_SQL.lower().split())
        self.assertEqual(normalized.count("order_item_current item"), 2)
        self.assertIn("scoped_items as materialized", normalized)
        self.assertIn("eligible_nights as materialized", normalized)
        for dataset in COST_SPIT_DATASETS:
            self.assertIn(f"'{dataset.lower()}'", normalized)

    def test_whole_stays_survive_the_window_edges(self):
        """Nights come from scoped_items, which has already read every
        SpaceOrder item in the window, so the per-reservation lookup that used
        to dominate this query is now only done for stays that reach past an
        edge. A stay truncated at an edge would invent its arrival and departure
        dates and over-allocate its cleaning share, so the top-up is what keeps
        the shortcut honest - measured identical on all seven datasets, and on a
        stay starting 2024-12-15, outside the window entirely."""
        normalized = " ".join(COST_SPIT_SQL.lower().split())

        # Nights are taken from the already-materialised item set...
        self.assertIn("window_nights as materialized", normalized)
        self.assertIn("join scoped_items item", normalized)
        # ...and only edge-touching stays go back to the source table.
        self.assertIn("edge_reservations as materialized", normalized)
        self.assertIn("having min(stay_date) <= %(start_date)s::date", normalized)
        self.assertIn("or max(stay_date) >= %(end_date)s::date", normalized)
        self.assertIn("edge_nights as materialized", normalized)
        # The union of both is what the rest of the query consumes, so a stay
        # recovered from the edge is indistinguishable from one that never left.
        self.assertIn("from window_nights union", normalized)
        self.assertIn("select reservation_id, stay_date from edge_nights", normalized)

    def test_http_reads_only_the_indexed_database_a_read_model(self):
        normalized = " ".join(COST_SPIT_READ_SQL.lower().split())
        self.assertIn("from functions.cost_spit_publication", normalized)
        self.assertIn("functions.cost_spit_daily", normalized)
        self.assertNotIn("order_item_current", normalized)
        # Read as text, not as json: the stored rows are already in the shape
        # and key case the response sends, so decoding them would only be
        # undone by re-encoding them on the way out.
        self.assertIn("daily.fact_rows::text", normalized)
        self.assertIn("daily.fact_count", normalized)

    def _read(self, rows, start, end, cutoff, published=None):
        fake_pool = FakePool(rows=rows)
        original_pool = cost_data_service.cost_pool
        original_ensure = cost_data_service.ensure_cost_settings_schema
        cost_data_service.cost_pool = fake_pool
        cost_data_service.ensure_cost_settings_schema = lambda: None
        try:
            return cost_data_service.fetch_cost_spit_data(
                start, end, cutoff, publication_version=published,
            ), fake_pool
        finally:
            cost_data_service.cost_pool = original_pool
            cost_data_service.ensure_cost_settings_schema = original_ensure

    def test_an_empty_lifecycle_result_is_available_not_missing(self):
        snapshot, fake_pool = self._read(
            [publication_row()],
            date(2025, 10, 1), date(2025, 10, 31), date(2025, 8, 19),
            published=7,
        )

        self.assertEqual(json.loads(snapshot.data_json), {
            dataset: [] for dataset in COST_SPIT_DATASETS
        })
        self.assertTrue(all(count == 0 for count in snapshot.row_counts.values()))
        self.assertEqual(len(fake_pool.executions), 1)
        for _query, parameters in fake_pool.executions:
            self.assertEqual(parameters["cutoff_date"], date(2025, 8, 19))

    def test_stored_rows_are_spliced_without_being_decoded(self):
        """The stored text is concatenated, not parsed. What has to hold is that
        the concatenation is still the JSON object the datasets describe."""
        snapshot, _pool = self._read(
            [
                publication_row(
                    dataset="roomRevenue",
                    stay_date=date(2025, 7, 1),
                    fact_count=2,
                    fact_rows=(
                        '[{"hotelName":"Hotel A","stayDate":"2025-07-01"},'
                        '{"hotelName":"Hotel B","stayDate":"2025-07-01"}]'
                    ),
                ),
                publication_row(
                    dataset="roomRevenue",
                    stay_date=date(2025, 7, 2),
                    fact_count=1,
                    fact_rows='[{"hotelName":"Hotel A","stayDate":"2025-07-02"}]',
                ),
                publication_row(
                    dataset="payments",
                    stay_date=date(2025, 7, 2),
                    fact_count=0,
                    fact_rows="[]",
                ),
            ],
            date(2025, 7, 1), date(2025, 7, 31), date(2025, 8, 19),
            published=8,
        )

        decoded = json.loads(snapshot.data_json)
        self.assertEqual(decoded["roomRevenue"], [
            {"hotelName": "Hotel A", "stayDate": "2025-07-01"},
            {"hotelName": "Hotel B", "stayDate": "2025-07-01"},
            {"hotelName": "Hotel A", "stayDate": "2025-07-02"},
        ])
        self.assertEqual(decoded["payments"], [])
        self.assertEqual(snapshot.row_counts["roomRevenue"], 3)
        self.assertEqual(snapshot.row_counts["payments"], 0)
        # Every dataset the contract names is present even when unpublished for
        # the range, so the browser never has to guard for a missing key.
        self.assertEqual(set(decoded), set(COST_SPIT_DATASETS))

    def test_a_publication_from_an_earlier_night_is_served_and_dated(self):
        cutoff = date(2025, 8, 19)
        snapshot, _pool = self._read(
            [publication_row(cutoff_date=cutoff - timedelta(days=2))],
            date(2025, 10, 1), date(2025, 10, 31), cutoff,
            published=9,
        )

        self.assertEqual(snapshot.cutoff_date, cutoff - timedelta(days=2))
        self.assertEqual(snapshot.stale_days, 2)

    def test_a_publication_older_than_the_allowance_is_unavailable(self):
        cutoff = date(2025, 8, 19)
        stale = cutoff - timedelta(
            days=cost_data_service.COST_SPIT_MAX_STALE_DAYS + 1
        )
        with self.assertRaisesRegex(
            cost_data_service.CostSpitUnavailableError, "behind the requested"
        ):
            self._read(
                [publication_row(cutoff_date=stale)],
                date(2025, 10, 1), date(2025, 10, 31), cutoff,
                published=10,
            )

    def test_a_publication_ahead_of_the_cutoff_is_refused(self):
        """Later than the point in time being asked for means it counts bookings
        that did not exist yet, which is the one error SPIT must never make."""
        cutoff = date(2025, 8, 19)
        with self.assertRaisesRegex(
            cost_data_service.CostSpitUnavailableError, "ahead of"
        ):
            self._read(
                [publication_row(cutoff_date=cutoff + timedelta(days=1))],
                date(2025, 10, 1), date(2025, 10, 31), cutoff,
                published=11,
            )

    def test_an_uncovered_range_does_not_fall_back_to_the_live_source(self):
        fake_pool = FakePool(rows=[publication_row(run_id=6)])
        original_pool = cost_data_service.cost_pool
        original_ensure = cost_data_service.ensure_cost_settings_schema
        cost_data_service.cost_pool = fake_pool
        cost_data_service.ensure_cost_settings_schema = lambda: None
        try:
            with self.assertRaisesRegex(
                cost_data_service.CostSpitUnavailableError,
                "does not cover",
            ):
                cost_data_service.fetch_cost_spit_data(
                    date(2024, 12, 1),
                    date(2025, 1, 31),
                    date(2025, 8, 19),
                    publication_version=9,
                )
        finally:
            cost_data_service.cost_pool = original_pool
            cost_data_service.ensure_cost_settings_schema = original_ensure

        self.assertEqual(len(fake_pool.executions), 1)


if __name__ == "__main__":
    unittest.main()
