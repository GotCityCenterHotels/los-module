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
            # A qualified lookup passes (table, schema) and must only see that
            # schema's table; an unqualified one passes (table,).
            table = parameters[0]
            if len(parameters) > 1:
                table = f"{parameters[1]}.{table}"
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

    def test_categories_are_ordered_by_the_mews_ordering_not_by_name(self):
        columns = {"resource_category_current": {
            "id", "enterprise_id", "space_name", "capacity", "extra_capacity",
            "type", "is_active", "ordering",
        }}
        rows = [{
            "category_id": "cat-1", "category_name": "Suite",
            "capacity": 2, "extra_capacity": 0, "category_ordering": 1,
        }, {
            "category_id": "cat-2", "category_name": "Double",
            "capacity": 2, "extra_capacity": 0, "category_ordering": 2,
        }]
        categories, cursor = self._run(columns, rows)

        query = " ".join(cursor.executed[-1].lower().split())
        self.assertIn('coalesce(category."ordering"', query)
        self.assertIn("order by category_ordering, category_name", query)
        # Source order is preserved, so the Mews ordering reaches the editor.
        self.assertEqual(
            [category["categoryName"] for category in categories],
            ["Suite", "Double"],
        )
        self.assertEqual(categories[0]["ordering"], 1)

    def test_a_mirror_without_the_ordering_column_falls_back_to_the_name(self):
        columns = {"resource_category_current": {
            "id", "enterprise_id", "space_name", "capacity", "extra_capacity",
            "type", "is_active",
        }}
        rows = [{
            "category_id": "cat-1", "category_name": "Double",
            "capacity": 2, "extra_capacity": 0,
        }]
        categories, cursor = self._run(columns, rows)

        query = " ".join(cursor.executed[-1].lower().split())
        self.assertIn("order by category_ordering, category_name", query)
        self.assertEqual(
            categories[0]["ordering"],
            cost_source_service.UNORDERED_CATEGORY_RANK,
        )


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


class SourceWindowTests(unittest.TestCase):
    """Every reservation-derived lookup is bounded.

    The channel picker used to read the hotel's entire reservation history -
    hundreds of thousands of rows aggregated down to a handful of strings - on
    every Cost Input page load, and it was the single largest cost in opening
    the page.
    """

    def setUp(self):
        cost_source_service._reset_column_cache()

    def _run(self, call, columns, rows):
        cursor = FakeCursor(columns, rows)
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ):
            return call(), cursor

    def test_the_channel_lookup_is_bounded_by_a_date_window(self):
        _, cursor = self._run(
            lambda: cost_source_service.list_channels("hotel-1"),
            {"reservation_current": {"id", "service_id", "origin"}},
            [{"channel_name": "ChannelManager"}],
        )
        query = " ".join(cursor.executed[-1].lower().split())

        self.assertIn("reservation.start_utc >= %(window_start)s", query)
        self.assertIn("limit", query)

    def test_origins_come_back_with_how_often_each_occurs(self):
        origins, cursor = self._run(
            lambda: cost_source_service.list_origins("hotel-1"),
            {"reservation_current": {"id", "service_id", "origin"}},
            [{"origin_name": "ChannelManager", "reservation_count": 1204}],
        )

        self.assertEqual(origins, [{
            "id": "ChannelManager",
            "name": "ChannelManager",
            "reservationCount": 1204,
        }])
        self.assertIn("start_utc >= %(window_start)s", " ".join(cursor.executed[-1].split()))

    def test_a_mirror_without_an_origin_column_degrades_to_no_origins(self):
        origins, _ = self._run(
            lambda: cost_source_service.list_origins("hotel-1"),
            {"reservation_current": {"id", "service_id"}},
            [],
        )
        self.assertEqual(origins, [])

    def test_a_mirror_without_an_agency_link_degrades_to_no_suggestions(self):
        # No travel_agency_id on the reservation and no company table: the
        # filter stays free text rather than failing the page.
        agencies, _ = self._run(
            lambda: cost_source_service.list_travel_agencies("hotel-1", search="exp"),
            {"reservation_current": {"id", "service_id", "origin"}},
            [],
        )
        self.assertEqual(agencies, [])


class MatchingRateTests(unittest.TestCase):
    def setUp(self):
        cost_source_service._reset_column_cache()

    def _run(self, columns, rows, **kwargs):
        cursor = FakeCursor(columns, rows)
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ):
            return cost_source_service.list_matching_rates("hotel-1", **kwargs), cursor

    def test_rates_are_narrowed_to_the_reservations_that_used_them(self):
        payload, cursor = self._run(
            {
                "reservation_current": {"id", "service_id", "origin", "rate_id"},
                "rate_current": {"id", "service_id", "rate_name"},
            },
            [{"rate_id": "r1", "rate_name": "BAR", "reservation_count": 42}],
            origins=["ChannelManager"],
        )

        self.assertEqual(payload["rates"], [
            {"id": "r1", "name": "BAR", "reservationCount": 42}
        ])
        self.assertTrue(payload["filtered"])
        self.assertTrue(payload["originFilterApplied"])
        query = " ".join(cursor.executed[-1].lower().split())
        self.assertIn("%(origins)s::text[]", query)

    def test_an_agency_term_the_mirror_cannot_honour_is_reported_not_ignored(self):
        # A full rate list returned as if it had been filtered reads as truth.
        payload, _ = self._run(
            {
                "reservation_current": {"id", "service_id", "origin", "rate_id"},
                "rate_current": {"id", "service_id", "rate_name"},
            },
            [{"rate_id": "r1", "rate_name": "BAR", "reservation_count": 42}],
            agencySearch="expedia",
        )

        self.assertFalse(payload["agencyFilterApplied"])

    def test_reservations_without_a_rate_fall_back_to_every_rate_on_the_property(self):
        payload, _ = self._run(
            {
                "reservation_current": {"id", "service_id", "origin"},
                "rate_current": {"id", "service_id", "rate_name", "is_active"},
            },
            [{"rate_id": "r9", "rate_name": "Corporate"}],
        )

        self.assertFalse(payload["filtered"])
        self.assertEqual(payload["rates"], [{"id": "r9", "name": "Corporate"}])


class ContainsPatternTests(unittest.TestCase):
    def test_like_metacharacters_in_the_search_term_are_escaped(self):
        # A search for "50%" must look for a literal per cent sign, not for
        # "anything".
        self.assertEqual(cost_source_service.contains_pattern("50%"), "%50\\%%")
        self.assertEqual(cost_source_service.contains_pattern("a_b"), "%a\\_b%")
        self.assertEqual(cost_source_service.contains_pattern("back\\slash"), "%back\\\\slash%")
        self.assertEqual(cost_source_service.contains_pattern("expedia"), "%expedia%")


class SourceCacheTests(unittest.TestCase):
    """One connection for the whole picker payload, memoized per worker."""

    def setUp(self):
        cost_source_service._reset_column_cache()

    def tearDown(self):
        cost_source_service._reset_column_cache()

    def test_every_lookup_shares_one_connection_and_the_result_is_cached(self):
        cursor = FakeCursor(
            {
                "rate_current": {"id", "service_id", "rate_name"},
                "reservation_current": {"id", "service_id", "origin", "rate_id"},
                "resource_category_current": {
                    "id", "enterprise_id", "space_name", "capacity", "type", "is_active",
                },
            },
            [],
        )
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ) as connect:
            first = cost_source_service.fetch_cost_sources("hotel-1")
            second = cost_source_service.fetch_cost_sources("hotel-1")

        # Four lookups used to mean four TLS handshakes; the second page load
        # used to mean four more.
        connect.assert_called_once()
        self.assertIs(first, second)
        self.assertIn("origins", first)
        self.assertTrue(first["capabilities"]["origin"])
        self.assertTrue(first["capabilities"]["rateFromReservations"])
        self.assertFalse(first["capabilities"]["travelAgency"])


class TravelAgencyTests(unittest.TestCase):
    """staging.travel_agency is the real source, and it is not on the search
    path - so the schema is part of the name everywhere it is used."""

    COLUMNS = {
        "reservation_current": {"id", "service_id", "origin", "travel_agency_id"},
        "staging.travel_agency": {"id", "name"},
    }

    def setUp(self):
        cost_source_service._reset_column_cache()

    def _run(self, call, columns, rows):
        cursor = FakeCursor(columns, rows)
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ):
            return call(), cursor

    def test_the_agency_search_joins_the_staging_table_schema_qualified(self):
        agencies, cursor = self._run(
            lambda: cost_source_service.list_travel_agencies("hotel-1", search="exp"),
            self.COLUMNS,
            [{"agency_name": "Expedia", "reservation_count": 812}],
        )

        self.assertEqual(agencies, [
            {"id": "Expedia", "name": "Expedia", "reservationCount": 812}
        ])
        query = " ".join(cursor.executed[-1].split())
        # Unqualified, this resolves to whatever the search path finds - which
        # for this table is nothing at all.
        self.assertIn('JOIN "staging"."travel_agency" agency', query)
        self.assertIn('ON agency."id" = reservation."travel_agency_id"', query)
        self.assertIn("ILIKE %(agency_pattern)s", query)

    def test_an_unqualified_probe_never_sees_the_staging_table(self):
        # The guard that keeps a same-named table in another schema from
        # unioning its columns in has to keep working now that a qualified
        # lookup exists.
        cursor = FakeCursor(self.COLUMNS, [])
        self.assertEqual(
            cost_source_service._table_columns(cursor, "travel_agency"), set()
        )
        self.assertEqual(
            cost_source_service._table_columns(cursor, "staging.travel_agency"),
            {"id", "name"},
        )

    def test_matching_rates_narrow_by_agency_through_the_same_join(self):
        columns = dict(self.COLUMNS)
        columns["reservation_current"] = columns["reservation_current"] | {"rate_id"}
        columns["rate_current"] = {"id", "service_id", "rate_name"}
        payload, cursor = self._run(
            lambda: cost_source_service.list_matching_rates(
                "hotel-1", origins=["ChannelManager"], agencySearch="expedia"
            ),
            columns,
            [{"rate_id": "r1", "rate_name": "BAR", "reservation_count": 12}],
        )

        self.assertTrue(payload["agencyFilterApplied"])
        self.assertTrue(payload["originFilterApplied"])
        query = " ".join(cursor.executed[-1].split())
        self.assertIn('JOIN "staging"."travel_agency" agency', query)

    def test_the_capability_flag_reports_the_agency_search_as_available(self):
        columns = dict(self.COLUMNS)
        columns["rate_current"] = {"id", "service_id", "rate_name"}
        columns["resource_category_current"] = {
            "id", "enterprise_id", "space_name", "capacity", "type", "is_active",
        }
        payload, _ = self._run(
            lambda: cost_source_service.fetch_cost_sources("hotel-1"), columns, []
        )

        self.assertTrue(payload["capabilities"]["travelAgency"])

    def test_a_column_added_to_the_source_is_picked_up_without_a_restart(self):
        # travel_agency_id was added to reservation_current after this code was
        # already running. Caching the column set for the life of the worker
        # meant an instance that had probed before the change kept reporting
        # the agency filter unavailable until it happened to recycle.
        columns = {"reservation_current": {"id", "service_id"}}
        cursor = FakeCursor(columns, [])
        with patch.object(
            cost_source_service, "get_export_connection",
            return_value=FakeConnection(cursor)
        ):
            self.assertIsNone(cost_source_service._resolve_column(
                cursor, "reservation_current",
                cost_source_service.RESERVATION_TRAVEL_AGENCY_COLUMNS,
                required=False,
            ))

            columns["reservation_current"] = {"id", "service_id", "travel_agency_id"}
            # Still cached, so still invisible.
            self.assertIsNone(cost_source_service._resolve_column(
                cursor, "reservation_current",
                cost_source_service.RESERVATION_TRAVEL_AGENCY_COLUMNS,
                required=False,
            ))

            with patch.object(
                cost_source_service, "COLUMN_CACHE_TTL_SECONDS", -1
            ):
                cost_source_service._column_cache.clear()
                self.assertEqual(
                    cost_source_service._resolve_column(
                        cursor, "reservation_current",
                        cost_source_service.RESERVATION_TRAVEL_AGENCY_COLUMNS,
                        required=False,
                    ),
                    "travel_agency_id",
                )

    def test_a_table_present_but_unreachable_degrades(self):
        # Present but with no key to join on is reported as unavailable, not
        # silently treated as "no agencies match".
        agencies, _ = self._run(
            lambda: cost_source_service.list_travel_agencies("hotel-1", search="exp"),
            {
                "reservation_current": {"id", "service_id", "travel_agency_id"},
                "staging.travel_agency": {"name"},
            },
            [],
        )
        self.assertEqual(agencies, [])

    def test_a_deployment_without_the_staging_table_still_degrades(self):
        agencies, _ = self._run(
            lambda: cost_source_service.list_travel_agencies("hotel-1", search="exp"),
            {"reservation_current": {"id", "service_id", "travel_agency_id"}},
            [],
        )
        self.assertEqual(agencies, [])


if __name__ == "__main__":
    unittest.main()
