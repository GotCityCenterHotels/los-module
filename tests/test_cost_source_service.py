import os
import unittest

from unittest.mock import patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from services import cost_source_service


class FakeCursor:
    """Answers information_schema probes, then the real query."""

    def __init__(self, columns_by_table, rows):
        self.columns_by_table = columns_by_table
        self.rows = rows
        self._result = []
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters=None):
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.executed.append(text)
        if "information_schema.columns" in text:
            table = parameters[0]
            self._result = [
                {"column_name": name}
                for name in self.columns_by_table.get(table, set())
            ]
        else:
            self._result = self.rows

    def fetchall(self):
        return self._result


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, **_kwargs):
        return self._cursor


class CleaningCategoryTests(unittest.TestCase):
    def setUp(self):
        cost_source_service._reset_column_cache()

    def _run(self, columns, rows):
        cursor = FakeCursor(columns, rows)
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ):
            return cost_source_service.list_cleaning_categories("hotel-1"), cursor

    def test_occupancy_steps_span_standard_plus_extra_capacity(self):
        columns = {"resource_category_current": {
            "id", "enterprise_id", "space_name", "capacity", "extra_capacity",
            "type", "is_active",
        }}
        rows = [{
            "category_id": "cat-1", "category_name": "Double Room",
            "capacity": 2, "extra_capacity": 1,
        }]
        categories, _ = self._run(columns, rows)
        self.assertEqual(categories[0]["occupancies"], [1, 2, 3])
        self.assertEqual(categories[0]["categoryName"], "Double Room")

    def test_category_without_extra_capacity_stops_at_standard(self):
        columns = {"resource_category_current": {
            "id", "enterprise_id", "space_name", "capacity", "extra_capacity",
            "type", "is_active",
        }}
        rows = [{
            "category_id": "cat-2", "category_name": "Single",
            "capacity": 1, "extra_capacity": 0,
        }]
        categories, _ = self._run(columns, rows)
        self.assertEqual(categories[0]["occupancies"], [1])

    def test_zero_capacity_category_is_skipped(self):
        columns = {"resource_category_current": {
            "id", "enterprise_id", "space_name", "capacity", "extra_capacity",
            "type", "is_active",
        }}
        rows = [{
            "category_id": "cat-3", "category_name": "Storage",
            "capacity": 0, "extra_capacity": 0,
        }]
        categories, _ = self._run(columns, rows)
        self.assertEqual(categories, [])

    def test_alternative_capacity_column_name_is_resolved(self):
        # The mirror may not use the Mews field name verbatim.
        columns = {"resource_category_current": {
            "id", "enterprise_id", "space_name", "normal_bed_count",
            "extra_bed_count", "type", "is_active",
        }}
        rows = [{
            "category_id": "cat-4", "category_name": "Twin",
            "capacity": 2, "extra_capacity": 0,
        }]
        categories, cursor = self._run(columns, rows)
        self.assertEqual(categories[0]["occupancies"], [1, 2])
        self.assertTrue(any("normal_bed_count" in sql for sql in cursor.executed))

    def test_missing_capacity_column_reports_what_was_tried(self):
        columns = {"resource_category_current": {"id", "enterprise_id", "space_name"}}
        cursor = FakeCursor(columns, [])
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ):
            with self.assertRaises(cost_source_service.CostSourceUnavailableError) as raised:
                cost_source_service.list_cleaning_categories("hotel-1")
        message = str(raised.exception)
        self.assertIn("capacity", message)
        self.assertIn("space_name", message)

    def test_unknown_table_is_reported_clearly(self):
        cursor = FakeCursor({}, [])
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ):
            with self.assertRaises(cost_source_service.CostSourceUnavailableError) as raised:
                cost_source_service.list_cleaning_categories("hotel-1")
        self.assertIn("resource_category_current", str(raised.exception))


class ChannelLookupTests(unittest.TestCase):
    def setUp(self):
        cost_source_service._reset_column_cache()

    def test_missing_channel_column_degrades_to_empty_list(self):
        # No channel-like column must not break the whole page; the match value
        # simply stays free text.
        cursor = FakeCursor({"reservation_current": {"id", "service_id"}}, [])
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ):
            self.assertEqual(cost_source_service.list_channels("hotel-1"), [])

    def test_origin_column_is_used_when_present(self):
        cursor = FakeCursor(
            {"reservation_current": {"id", "service_id", "origin"}},
            [{"channel_name": "ChannelManager"}, {"channel_name": "Direct"}],
        )
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ):
            channels = cost_source_service.list_channels("hotel-1")
        self.assertEqual([c["name"] for c in channels], ["ChannelManager", "Direct"])


class RateLookupTests(unittest.TestCase):
    def setUp(self):
        cost_source_service._reset_column_cache()

    def test_rates_join_through_service_when_no_enterprise_column(self):
        cursor = FakeCursor(
            {"rate_current": {"id", "service_id", "rate_name", "is_active"}},
            [{"rate_id": "r1", "rate_name": "BAR"}],
        )
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ):
            rates = cost_source_service.list_rates("hotel-1")
        self.assertEqual(rates, [{"id": "r1", "name": "BAR"}])
        self.assertTrue(any("service_current" in sql for sql in cursor.executed))

    def test_denormalised_enterprise_column_skips_the_join(self):
        cursor = FakeCursor(
            {"rate_current": {"id", "enterprise_id", "name", "is_active"}},
            [{"rate_id": "r2", "rate_name": "Corporate"}],
        )
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ):
            rates = cost_source_service.list_rates("hotel-1")
        self.assertEqual(rates, [{"id": "r2", "name": "Corporate"}])
        query = [sql for sql in cursor.executed if "rate_current" in sql][-1]
        self.assertNotIn("service_current", query)


if __name__ == "__main__":
    unittest.main()
