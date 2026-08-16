import os
import re
import unittest
from decimal import Decimal
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

from services import cost_settings_service


cost_settings_service.cost_pool.close()


class CostSettingsValidationTests(unittest.TestCase):
    def test_property_list_comes_from_enterprise_current(self):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, sql): self.sql = sql
            def fetchall(self):
                return [{
                    "enterprise_id": "00000000-0000-0000-0000-000000000001",
                    "hotel_name": "Hotel A",
                }]

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def cursor(self): self.cursor_instance = Cursor(); return self.cursor_instance

        connection = Connection()
        with patch.object(
            cost_settings_service,
            "ensure_cost_settings_schema",
        ), patch.object(
            cost_settings_service,
            "_list_mirrored_properties",
            return_value=[],
        ), patch.object(
            cost_settings_service,
            "get_export_connection",
            return_value=connection,
        ), patch.object(
            cost_settings_service,
            "_upsert_mirrored_properties",
        ) as mirror, patch.object(
            cost_settings_service,
            "_preload_property_settings",
        ) as preload:
            result = cost_settings_service.list_cost_settings_hotels()

        self.assertEqual(result, [{
            "enterpriseId": "00000000-0000-0000-0000-000000000001",
            "hotelName": "Hotel A",
        }])
        self.assertIn("FROM enterprise_current", connection.cursor_instance.sql)
        mirror.assert_called_once_with(result)
        preload.assert_called_once_with(result)

    def test_property_lookup_uses_source_database_and_text_id_comparison(self):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, sql, parameters):
                self.sql = sql
                self.parameters = parameters
            def fetchone(self):
                return {
                    "enterprise_id": "property-42",
                    "hotel_name": "Hotel A",
                }

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def cursor(self): self.cursor_instance = Cursor(); return self.cursor_instance

        connection = Connection()
        with patch.object(
            cost_settings_service,
            "_get_mirrored_property",
            return_value=None,
        ), patch.object(
            cost_settings_service,
            "get_export_connection",
            return_value=connection,
        ) as source_connection, patch.object(
            cost_settings_service,
            "_upsert_mirrored_properties",
        ) as mirror:
            result = cost_settings_service._get_cost_settings_hotel("property-42")

        source_connection.assert_called_once_with()
        self.assertEqual(result["hotelName"], "Hotel A")
        self.assertIn("id::text = %s", connection.cursor_instance.sql)
        self.assertEqual(connection.cursor_instance.parameters, ("property-42",))
        mirror.assert_called_once_with([result])

    def test_property_list_prefers_database_a_mirror(self):
        mirrored = [{"enterpriseId": "property-42", "hotelName": "Hotel A"}]

        with patch.object(
            cost_settings_service,
            "ensure_cost_settings_schema",
        ), patch.object(
            cost_settings_service,
            "_list_mirrored_properties",
            return_value=mirrored,
        ), patch.object(
            cost_settings_service,
            "_list_source_properties",
        ) as source, patch.object(
            cost_settings_service,
            "_preload_property_settings",
        ) as preload:
            result = cost_settings_service.list_cost_settings_hotels()

        self.assertEqual(result, mirrored)
        source.assert_not_called()
        preload.assert_called_once_with(mirrored)

    def test_property_list_falls_back_to_imported_cost_data(self):
        imported = [{"enterpriseId": "property-42", "hotelName": "Hotel A"}]

        with patch.object(
            cost_settings_service,
            "_list_mirrored_properties",
            return_value=[],
        ), patch.object(
            cost_settings_service,
            "_list_source_properties",
            side_effect=RuntimeError("source unavailable"),
        ), patch.object(
            cost_settings_service,
            "_list_imported_properties",
            return_value=imported,
        ) as imported_properties, patch.object(
            cost_settings_service,
            "ensure_cost_settings_schema",
        ), patch.object(
            cost_settings_service,
            "_preload_property_settings",
        ) as preload:
            result = cost_settings_service.list_cost_settings_hotels()

        self.assertEqual(result, imported)
        imported_properties.assert_called_once_with()
        preload.assert_called_once_with(imported)

    def test_property_lookup_falls_back_to_imported_cost_data(self):
        imported = {"enterpriseId": "property-42", "hotelName": "Hotel A"}

        with patch.object(
            cost_settings_service,
            "_get_mirrored_property",
            return_value=None,
        ), patch.object(
            cost_settings_service,
            "get_export_connection",
            side_effect=RuntimeError("source unavailable"),
        ), patch.object(
            cost_settings_service,
            "_get_imported_property",
            return_value=imported,
        ) as imported_property:
            result = cost_settings_service._get_cost_settings_hotel("property-42")

        self.assertEqual(result, imported)
        imported_property.assert_called_once_with("property-42")

    def test_selected_property_pair_survives_a_repeat_lookup_miss(self):
        with patch.object(
            cost_settings_service,
            "_get_preloaded_property",
            return_value=None,
        ), patch.object(
            cost_settings_service,
            "_get_cost_settings_hotel",
            side_effect=ValueError("not found"),
        ):
            result = cost_settings_service._resolve_cost_settings_hotel(
                "property-42",
                "Hotel A",
            )

        self.assertEqual(result, {
            "enterpriseId": "property-42",
            "hotelName": "Hotel A",
        })

    def test_repeat_lookup_miss_without_selected_name_is_rejected(self):
        with patch.object(
            cost_settings_service,
            "_get_preloaded_property",
            return_value=None,
        ), patch.object(
            cost_settings_service,
            "_get_cost_settings_hotel",
            side_effect=ValueError("not found"),
        ), self.assertRaisesRegex(ValueError, "not found"):
            cost_settings_service._resolve_cost_settings_hotel("property-42")

    def test_preloaded_property_avoids_a_second_source_lookup(self):
        preloaded = {
            "enterpriseId": "7b09bedb-2aeb-4855-b5d4-ac1700c0605a",
            "hotelName": "Hotel Vasa",
        }
        with patch.object(
            cost_settings_service,
            "_get_preloaded_property",
            return_value=preloaded,
        ) as local_lookup, patch.object(
            cost_settings_service,
            "_get_cost_settings_hotel",
        ) as source_lookup:
            result = cost_settings_service._resolve_cost_settings_hotel(
                preloaded["enterpriseId"]
            )

        self.assertEqual(result, preloaded)
        local_lookup.assert_called_once_with(preloaded["enterpriseId"])
        source_lookup.assert_not_called()

    def test_property_preload_inserts_defaults_without_overwriting_settings(self):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def executemany(self, sql, parameters):
                self.sql = sql
                self.parameters = parameters

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def cursor(self): self.cursor_instance = Cursor(); return self.cursor_instance

        class Pool:
            def __init__(self): self.connection_instance = Connection()
            def connection(self): return self.connection_instance

        pool = Pool()
        properties = [
            {"enterpriseId": "property-42", "hotelName": "Hotel A"},
            {"enterpriseId": "property-43", "hotelName": "Hotel B"},
        ]
        # The preload remembers what this worker has already written, so an
        # earlier test touching the same ids would otherwise make this one see
        # no write at all - and the order tests run in is not fixed.
        cost_settings_service._reset_property_memo()
        with patch.object(
            cost_settings_service,
            "ensure_cost_settings_schema",
        ) as ensure_schema, patch.object(
            cost_settings_service,
            "cost_pool",
            pool,
        ):
            cost_settings_service._preload_property_settings(properties)

        ensure_schema.assert_called_once_with()
        cursor = pool.connection_instance.cursor_instance
        self.assertEqual(cursor.parameters, [
            ("property-42",),
            ("property-43",),
        ])
        self.assertIn("INSERT INTO functions.cost_property_settings", cursor.sql)
        self.assertIn("ON CONFLICT (enterprise_id) DO NOTHING", cursor.sql)
        self.assertNotIn("card_cost_percent", cursor.sql)

    def test_settings_load_persists_property_pair_in_database_a(self):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, sql, parameters=None): pass
            def fetchone(self): return None
            def fetchall(self): return []

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def cursor(self, **kwargs): return Cursor()

        class Pool:
            def connection(self): return Connection()

        property_record = {
            "enterpriseId": "property-42",
            "hotelName": "Hotel A",
        }
        with patch.object(
            cost_settings_service,
            "ensure_cost_settings_schema",
        ), patch.object(
            cost_settings_service,
            "_upsert_mirrored_properties",
        ) as mirror, patch.object(
            cost_settings_service,
            "_preload_property_settings",
        ) as preload, patch.object(
            cost_settings_service,
            "cost_pool",
            Pool(),
        ):
            result = cost_settings_service.fetch_cost_settings(
                property_record["enterpriseId"],
                property_record["hotelName"],
            )

        mirror.assert_called_once_with([property_record])
        preload.assert_called_once_with([property_record])
        self.assertEqual(result["enterpriseId"], "property-42")
        self.assertEqual(result["hotelName"], "Hotel A")

    def test_defaults_include_two_percent_card_cost(self):
        result = cost_settings_service.validate_cost_settings(
            "00000000-0000-0000-0000-000000000001", "Hotel A", {}
        )

        self.assertEqual(result["profile"]["cardCostPercent"], 2)
        self.assertEqual(result["profile"]["breakfastCalculationBasis"], "guests")

    def test_enterprise_ids_are_treated_as_opaque_source_keys(self):
        result = cost_settings_service.validate_cost_settings(
            "property-42", "Hotel A", {}
        )

        self.assertEqual(result["enterpriseId"], "property-42")

    def test_complete_property_configuration_is_normalized(self):
        result = cost_settings_service.validate_cost_settings("00000000-0000-0000-0000-000000000001", " Hotel A ", {
            "profile": {"currency": "sek", "cardCostPercent": "2.5", "breakfastCalculationBasis": "products"},
            "distributionGroups": [{
                "groupName": "OTA", "costPercent": "14.5",
                "rules": [{"matchType": "channel", "matchValue": "Booking.com"}],
            }],
            # One row per (room category, occupancy).
            "cleaningCategories": [
                {"categoryName": "Double", "resourceCategoryId": "cat-1", "occupancy": 1, "cleaningMinutes": 30, "linenCost": 75},
                {"categoryName": "Double", "resourceCategoryId": "cat-1", "occupancy": 2, "cleaningMinutes": 40, "linenCost": 150},
            ],
            "arrivalTiers": [{"minArrivals": 30, "maxArrivals": "", "receptionHours": 4}],
            "breakfastTiers": [{"minGuests": 0, "maxGuests": 49, "staffHours": 0}, {"minGuests": 50, "maxGuests": 70, "staffHours": 4}],
        })

        self.assertEqual(result["hotelName"], "Hotel A")
        self.assertEqual(result["profile"]["currency"], "SEK")
        self.assertEqual(result["arrivalTiers"][0]["maxArrivals"], None)
        # The same category at two occupancies is valid; it is no longer a
        # duplicate name clash the way the old guest-band model treated it.
        self.assertEqual(len(result["cleaningCategories"]), 2)
        self.assertEqual(result["cleaningCategories"][1]["occupancy"], 2)

    def test_same_category_and_occupancy_twice_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "only appear once"):
            cost_settings_service.validate_cost_settings("00000000-0000-0000-0000-000000000001", "Hotel A", {
                "cleaningCategories": [
                    {"categoryName": "Double", "occupancy": 2, "cleaningMinutes": 30, "linenCost": 75},
                    {"categoryName": "double", "occupancy": 2, "cleaningMinutes": 45, "linenCost": 90},
                ],
            })

    def test_overlapping_thresholds_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "ranges cannot overlap"):
            cost_settings_service.validate_cost_settings("00000000-0000-0000-0000-000000000001", "Hotel A", {
                "breakfastTiers": [
                    {"minGuests": 0, "maxGuests": 50, "staffHours": 0},
                    {"minGuests": 50, "maxGuests": 70, "staffHours": 4},
                ]
            })

    def test_percentages_above_one_hundred_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            cost_settings_service.validate_cost_settings("00000000-0000-0000-0000-000000000001", "Hotel A", {
                "profile": {"cardCostPercent": 101}
            })


class MoneyRoundingTests(unittest.TestCase):
    """SEK is whole kronor everywhere, so it is stored that way."""

    def test_money_fields_are_stored_as_whole_kronor(self):
        result = cost_settings_service.validate_cost_settings(
            "property-42", "Hotel A", {
                "profile": {
                    "cleaningCostPerMinute": "5.49",
                    "receptionCostPerHour": "312.50",
                    "breakfastFoodCostPerGuest": "41.4",
                    "breakfastStaffCostPerHour": "249.5",
                },
                "cleaningCategories": [{
                    "categoryName": "Double", "occupancy": 1,
                    "cleaningMinutes": "22.5", "linenCost": "74.6",
                }],
            }
        )

        profile = result["profile"]
        self.assertEqual(profile["cleaningCostPerMinute"], Decimal("5"))
        # .50 rounds up, so a half krona never disappears into the floor.
        self.assertEqual(profile["receptionCostPerHour"], Decimal("313"))
        self.assertEqual(profile["breakfastFoodCostPerGuest"], Decimal("41"))
        self.assertEqual(profile["breakfastStaffCostPerHour"], Decimal("250"))
        self.assertEqual(result["cleaningCategories"][0]["linenCost"], Decimal("75"))
        # Minutes and percentages are not money and keep their precision.
        self.assertEqual(
            result["cleaningCategories"][0]["cleaningMinutes"], Decimal("22.5")
        )

    def test_percentages_keep_their_decimals(self):
        result = cost_settings_service.validate_cost_settings(
            "property-42", "Hotel A", {"profile": {"cardCostPercent": "2.75"}}
        )
        self.assertEqual(result["profile"]["cardCostPercent"], Decimal("2.75"))

    def test_out_of_range_percentages_are_still_rejected_before_rounding(self):
        # 100.4 must not become a valid 100 on the way through.
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            cost_settings_service.validate_cost_settings(
                "property-42", "Hotel A", {"profile": {"cardCostPercent": "100.4"}}
            )

    def test_the_money_field_set_matches_the_shared_frontend_registry(self):
        registry = Path(__file__).resolve().parent.parent / "frontend" / "los-format.js"
        source = registry.read_text(encoding="utf-8")
        block = source.split("MONEY_FIELDS = Object.freeze(new Set([", 1)[1]
        block = block.split("]))", 1)[0]
        frontend_fields = set(re.findall(r'"([A-Za-z]+)"', block))

        self.assertEqual(frontend_fields, set(cost_settings_service.MONEY_FIELDS))


class BulkCostSettingsTests(unittest.TestCase):
    """The dashboard costs every property in one round trip."""

    class Cursor:
        def __init__(self, results):
            self.results = results
            self.executed = []
            self._result = []

        def __enter__(self): return self
        def __exit__(self, *args): return False

        def execute(self, sql, parameters=None):
            self.executed.append(sql)
            self._result = self.results.pop(0) if self.results else []

        def fetchall(self): return self._result

    class Connection:
        def __init__(self, cursor): self._cursor = cursor
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def cursor(self, **_kwargs): return self._cursor

    def test_settings_are_keyed_by_hotel_name_with_their_own_collections(self):
        cursor = self.Cursor([
            [
                {"enterprise_id": "p-1", "hotel_name": "Hotel A",
                 "currency": "SEK", "card_cost_percent": Decimal("2.5")},
                {"enterprise_id": "p-2", "hotel_name": "Hotel B",
                 "currency": "SEK"},
            ],
            [{"enterprise_id": "p-1", "group_name": "OTA",
              "cost_percent": Decimal("14"), "rules": []}],
            [{"enterprise_id": "p-1", "category_name": "Double", "occupancy": 1,
              "resource_category_id": "cat-1", "cleaning_minutes": Decimal("30"),
              "linen_cost": Decimal("75")}],
            [{"enterprise_id": "p-2", "min_arrivals": 0, "max_arrivals": None,
              "reception_hours": Decimal("8")}],
            [{"enterprise_id": "p-1", "min_guests": 0, "max_guests": 30,
              "staff_hours": Decimal("4")}],
        ])

        class Pool:
            def connection(inner): return BulkCostSettingsTests.Connection(cursor)

        with patch.object(
            cost_settings_service, "ensure_cost_settings_schema",
        ), patch.object(cost_settings_service, "cost_pool", Pool()):
            result = cost_settings_service.fetch_all_cost_settings()

        self.assertEqual(sorted(result), ["Hotel A", "Hotel B"])
        self.assertEqual(result["Hotel A"]["enterpriseId"], "p-1")
        self.assertEqual(result["Hotel A"]["profile"]["cardCostPercent"], "2.5")
        # A property that saved nothing gets defaults for the profile only, and
        # empty collections - never another property's rows.
        self.assertEqual(result["Hotel B"]["profile"]["cardCostPercent"], "2")
        self.assertEqual(result["Hotel A"]["distributionGroups"][0]["groupName"], "OTA")
        self.assertEqual(result["Hotel B"]["distributionGroups"], [])
        self.assertEqual(result["Hotel A"]["cleaningCategories"][0]["linenCost"], "75")
        self.assertEqual(result["Hotel B"]["cleaningCategories"], [])
        self.assertEqual(result["Hotel A"]["arrivalTiers"], [])
        self.assertEqual(result["Hotel B"]["arrivalTiers"][0]["receptionHours"], "8")
        self.assertEqual(result["Hotel A"]["breakfastTiers"][0]["maxGuests"], 30)
        # enterprise_id is the join key, not part of the payload.
        self.assertNotIn("enterpriseId", result["Hotel A"]["profile"])

    def test_every_collection_is_read_in_the_mews_category_ordering(self):
        cursor = self.Cursor([[], [], [], [], []])

        class Pool:
            def connection(inner): return BulkCostSettingsTests.Connection(cursor)

        with patch.object(
            cost_settings_service, "ensure_cost_settings_schema",
        ), patch.object(cost_settings_service, "cost_pool", Pool()):
            cost_settings_service.fetch_all_cost_settings()

        cleaning = " ".join(
            cost_settings_service.COLLECTION_QUERIES["cleaningCategories"].lower().split()
        )
        # The rows are now aliased because each one carries its beds, but
        # sort_order still comes before the name: it holds the Mews category
        # ordering captured when the rows were saved, and ordering by name here
        # would undo it on every reload.
        self.assertIn(
            "order by c.enterprise_id, c.sort_order, c.category_name", cleaning
        )
        self.assertIn("cost_cleaning_beds", cleaning)


class NumericGuardTests(unittest.TestCase):
    def test_overlapping_open_ended_tiers_report_a_validation_error(self):
        # Two tiers sharing a minimum where one is open-ended used to compare
        # None with int inside sorted(), raising TypeError -> HTTP 500 instead
        # of a 400 the user could act on.
        rows = [
            {"minGuests": 1, "maxGuests": None},
            {"minGuests": 1, "maxGuests": 5},
        ]
        with self.assertRaises(ValueError) as raised:
            cost_settings_service._validate_ranges(
                rows, "minGuests", "maxGuests", "Cleaning"
            )
        self.assertIn("overlap", str(raised.exception).lower())

    def test_disjoint_open_ended_tier_sorts_last_and_validates(self):
        rows = [
            {"minGuests": 5, "maxGuests": None},
            {"minGuests": 1, "maxGuests": 4},
        ]
        self.assertEqual(
            cost_settings_service._validate_ranges(
                rows, "minGuests", "maxGuests", "Cleaning"
            ),
            [(5, None), (1, 4)],
        )

    def test_non_finite_amounts_are_rejected(self):
        # Decimal("Infinity") is not < 0 and money fields have no maximum, so it
        # passed every guard and PostgreSQL numeric would store it.
        for value in ("Infinity", "-Infinity", "NaN"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    cost_settings_service._number(value, "Cleaning cost")

    def test_ordinary_amounts_still_pass(self):
        self.assertEqual(
            cost_settings_service._number("12.50", "Cleaning cost"),
            cost_settings_service.Decimal("12.50"),
        )


class ArrivalAndFranchiseProfileTests(unittest.TestCase):
    """The two new switches, and the fields the franchise switch governs."""

    def _profile(self, **overrides):
        return cost_settings_service.validate_cost_settings(
            "property-42", "Hotel A", {"profile": overrides}
        )["profile"]

    def test_arrivals_default_on_and_franchise_defaults_off(self):
        profile = self._profile()

        # A property saved before these fields existed must keep costing
        # arrivals, and must not acquire a franchise fee it never agreed to.
        self.assertIs(profile["arrivalCostEnabled"], True)
        self.assertIs(profile["franchiseEnabled"], False)
        self.assertEqual(profile["franchisePercent"], Decimal("0"))
        self.assertEqual(profile["franchiseBasis"], "net")
        self.assertEqual(profile["franchiseRevenueBase"], "roomInclProducts")
        self.assertEqual(profile["franchiseVatPercent"], Decimal("12"))

    def test_the_string_false_switches_a_cost_off(self):
        # A form post sends "on" or nothing; JSON sends a real bool. "false" is
        # a non-empty string and would otherwise be truthy.
        self.assertIs(self._profile(arrivalCostEnabled="false")["arrivalCostEnabled"], False)
        self.assertIs(self._profile(arrivalCostEnabled=False)["arrivalCostEnabled"], False)
        self.assertIs(self._profile(franchiseEnabled="on")["franchiseEnabled"], True)
        self.assertIs(self._profile(franchiseEnabled=True)["franchiseEnabled"], True)

    def test_franchise_basis_and_revenue_base_are_constrained(self):
        with self.assertRaisesRegex(ValueError, "franchiseBasis must be one of"):
            self._profile(franchiseBasis="incl-vat")
        with self.assertRaisesRegex(ValueError, "franchiseRevenueBase must be one of"):
            self._profile(franchiseRevenueBase="everything")
        # The database CHECK accepts exactly these four.
        for base in (
            "roomInclProducts", "roomExclProducts",
            "roomExclProductsPlusParking", "totalRevenue",
        ):
            self.assertEqual(
                self._profile(franchiseRevenueBase=base)["franchiseRevenueBase"], base
            )

    def test_franchise_percentages_are_bounded_like_every_other_percentage(self):
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            self._profile(franchisePercent="101")
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            self._profile(franchiseVatPercent="150")


class DistributionTreeValidationTests(unittest.TestCase):
    """Origin group -> travel agency subgroup -> rate group."""

    def _tree(self, groups):
        return cost_settings_service.validate_cost_settings(
            "property-42", "Hotel A", {"distributionOriginGroups": groups}
        )["distributionOriginGroups"]

    def test_a_complete_tree_is_normalized(self):
        result = self._tree([{
            "groupName": " Channel manager ",
            "fallbackPercent": "15",
            "origins": ["ChannelManager", "channelmanager", " "],
            "agencyGroups": [{
                "groupName": "Expedia",
                "fallbackPercent": "12.5",
                "filters": [
                    {"matchField": "travelAgency", "containsValue": " expedia "},
                    {"matchField": "travelAgency", "containsValue": "EXPEDIA"},
                    {"matchField": "travelAgency", "containsValue": "  "},
                ],
                "rateGroups": [{
                    "groupName": "Package",
                    "costPercent": "9",
                    "rates": [
                        {"rateId": "r1", "rateName": "BAR"},
                        {"rateId": "", "rateName": "bar"},
                        {"rateId": None, "rateName": "Corporate"},
                    ],
                }],
            }],
        }])

        group = result[0]
        self.assertEqual(group["groupName"], "Channel manager")
        self.assertEqual(group["fallbackPercent"], Decimal("15"))
        # The same origin twice in one group is one origin, not two.
        self.assertEqual(group["origins"], ["ChannelManager"])
        agency = group["agencyGroups"][0]
        self.assertEqual(
            [rule["containsValue"] for rule in agency["filters"]], ["expedia"]
        )
        rate_group = agency["rateGroups"][0]
        self.assertEqual(
            [rate["rateName"] for rate in rate_group["rates"]], ["BAR", "Corporate"]
        )
        self.assertEqual(rate_group["rates"][0]["rateId"], "r1")
        self.assertIsNone(rate_group["rates"][1]["rateId"])

    def test_a_group_that_matches_nothing_is_rejected(self):
        # Saved as-is it would either swallow every reservation or none of
        # them, depending on how the cost algorithm reads an empty filter.
        with self.assertRaisesRegex(ValueError, "no origins and no subgroups"):
            self._tree([{"groupName": "Empty", "fallbackPercent": "10"}])

        with self.assertRaisesRegex(ValueError, "no travel agency filter and no rate"):
            self._tree([{
                "groupName": "Channel manager",
                "origins": ["ChannelManager"],
                "agencyGroups": [{"groupName": "Nothing", "fallbackPercent": "5"}],
            }])

    def test_a_rate_group_without_rates_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "has no rates"):
            self._tree([{
                "groupName": "Channel manager",
                "origins": ["ChannelManager"],
                "agencyGroups": [{
                    "groupName": "Expedia",
                    "filters": [{"matchField": "travelAgency", "containsValue": "expedia"}],
                    "rateGroups": [{"groupName": "Package", "costPercent": "9", "rates": []}],
                }],
            }])

    def test_names_must_be_unique_within_their_own_level(self):
        def two_groups(name_one, name_two):
            return [
                {"groupName": name_one, "fallbackPercent": "1", "origins": ["A"]},
                {"groupName": name_two, "fallbackPercent": "2", "origins": ["B"]},
            ]

        # Uniqueness is per level and case-insensitive, matching the database
        # UNIQUE constraints.
        with self.assertRaisesRegex(ValueError, "Distribution group names must be unique"):
            self._tree(two_groups("OTA", "ota"))
        self.assertEqual(len(self._tree(two_groups("OTA", "Direct"))), 2)

        with self.assertRaisesRegex(ValueError, "names must be unique"):
            self._tree([{
                "groupName": "OTA",
                "origins": ["A"],
                "agencyGroups": [
                    {"groupName": "Expedia", "filters": [{"containsValue": "x"}]},
                    {"groupName": "expedia", "filters": [{"containsValue": "y"}]},
                ],
            }])

    def test_an_unknown_filter_field_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "filter field must be one of"):
            self._tree([{
                "groupName": "OTA",
                "origins": ["A"],
                "agencyGroups": [{
                    "groupName": "Expedia",
                    "filters": [{"matchField": "guestName", "containsValue": "x"}],
                }],
            }])

    def test_the_tree_is_optional_and_defaults_to_empty(self):
        result = cost_settings_service.validate_cost_settings(
            "property-42", "Hotel A", {}
        )
        self.assertEqual(result["distributionOriginGroups"], [])


class DistributionTreeWriteTests(unittest.TestCase):
    """The tree is rewritten parent-first, because each child keys off an
    identity column that only exists once its parent row is in."""

    class Cursor:
        def __init__(self):
            self.executed = []
            self.batches = []
            self._next_id = 0

        def execute(self, sql, parameters=None):
            self.executed.append((" ".join(str(sql).split()), parameters))

        def executemany(self, sql, parameters):
            self.batches.append((" ".join(str(sql).split()), list(parameters)))

        def fetchone(self):
            self._next_id += 1
            return (self._next_id,)

    def test_every_level_is_written_with_its_parents_returned_id(self):
        cursor = self.Cursor()
        cost_settings_service._insert_distribution_tree(cursor, "property-42", [{
            "groupName": "Channel manager",
            "fallbackPercent": Decimal("15"),
            "origins": ["ChannelManager"],
            "agencyGroups": [{
                "groupName": "Expedia",
                "fallbackPercent": Decimal("12"),
                "filters": [{"matchField": "travelAgency", "containsValue": "expedia"}],
                "rateGroups": [{
                    "groupName": "Package",
                    "costPercent": Decimal("9"),
                    "rates": [{"rateId": "r1", "rateName": "BAR"}],
                }],
            }],
        }])

        inserts = [sql for sql, _ in cursor.executed]
        self.assertTrue(any("cost_distribution_origin_groups" in sql for sql in inserts))
        self.assertTrue(any("cost_distribution_agency_groups" in sql for sql in inserts))
        self.assertTrue(any("cost_distribution_rate_groups" in sql for sql in inserts))

        # Identity ids come back 1, 2, 3 in parent-first order, so each child
        # batch must carry the id of the row inserted immediately above it.
        batches = {
            sql.split("functions.")[1].split(" ")[0]: rows
            for sql, rows in cursor.batches
        }
        self.assertEqual(batches["cost_distribution_origin_values"], [(1, "ChannelManager")])
        self.assertEqual(
            batches["cost_distribution_agency_filters"], [(2, "travelAgency", "expedia")]
        )
        self.assertEqual(batches["cost_distribution_rate_values"], [(3, "r1", "BAR")])

    def test_an_empty_tree_writes_nothing(self):
        cursor = self.Cursor()
        cost_settings_service._insert_distribution_tree(cursor, "property-42", [])

        self.assertEqual(cursor.executed, [])
        self.assertEqual(cursor.batches, [])


class DistributionTreeShapeTests(unittest.TestCase):
    """The settings endpoint is anonymous, so every nested level checks its
    own shape rather than trusting the payload."""

    def _tree(self, groups):
        return cost_settings_service.validate_cost_settings(
            "property-42", "Hotel A", {"distributionOriginGroups": groups}
        )["distributionOriginGroups"]

    def test_a_string_where_a_list_of_origins_belongs_is_rejected(self):
        # Iterating the string walked it character by character and saved nine
        # single-letter origins, returning 200 on a corrupted rulebook.
        with self.assertRaisesRegex(ValueError, "origins must be a list"):
            self._tree([{"groupName": "G", "origins": "ChannelManager"}])

    def test_a_non_text_origin_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "origins must be text"):
            self._tree([{"groupName": "G", "origins": [{"name": "A"}]}])

    def test_a_string_where_an_object_belongs_is_a_validation_error_not_a_crash(self):
        # These used to raise AttributeError, which the route maps to 500
        # rather than to the 400 a malformed request deserves.
        with self.assertRaisesRegex(ValueError, "must be an object"):
            self._tree([{"groupName": "G", "origins": ["A"], "agencyGroups": ["Expedia"]}])

        with self.assertRaisesRegex(ValueError, "must be an object"):
            self._tree([{
                "groupName": "G",
                "origins": ["A"],
                "agencyGroups": [{
                    "groupName": "Expedia",
                    "filters": [{"containsValue": "x"}],
                    "rateGroups": [{"groupName": "R", "rates": ["BAR"]}],
                }],
            }])

    def test_the_whole_tree_must_be_a_list(self):
        with self.assertRaisesRegex(ValueError, "must be a list"):
            self._tree({"groupName": "G"})


class BedTypeValidationTests(unittest.TestCase):
    def _settings(self, payload):
        return cost_settings_service.validate_cost_settings(
            "property-42", "Hotel A", payload
        )

    def test_bed_types_carry_the_linen_cost_in_whole_kronor(self):
        result = self._settings({"bedTypes": [
            {"bedName": " Double bed ", "linenCost": "74.6"},
            {"bedName": "Extra bed", "linenCost": 40},
        ]})

        self.assertEqual(result["bedTypes"], [
            {"bedName": "Double bed", "linenCost": Decimal("75")},
            {"bedName": "Extra bed", "linenCost": Decimal("40")},
        ])

    def test_duplicate_bed_names_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Bed type names must be unique"):
            self._settings({"bedTypes": [
                {"bedName": "Double bed", "linenCost": 75},
                {"bedName": "double bed", "linenCost": 40},
            ]})

    def test_a_room_cannot_reference_a_bed_the_property_has_not_defined(self):
        # The row references the bed by name, so an unknown one would look
        # configured in the editor while contributing no linen cost at all.
        with self.assertRaisesRegex(ValueError, "not one of this property's bed types"):
            self._settings({
                "bedTypes": [{"bedName": "Double bed", "linenCost": 75}],
                "cleaningCategories": [{
                    "categoryName": "Double", "occupancy": 1,
                    "beds": [{"bedName": "Bunk", "quantity": 1}],
                }],
            })

    def test_blank_minutes_are_stored_as_absent_rather_than_zero(self):
        result = self._settings({"cleaningCategories": [
            {"categoryName": "Double", "occupancy": 1, "cleaningMinutes": "30"},
            {"categoryName": "Double", "occupancy": 2, "cleaningMinutes": ""},
        ]})

        self.assertEqual(result["cleaningCategories"][0]["cleaningMinutes"], Decimal("30"))
        self.assertIsNone(result["cleaningCategories"][1]["cleaningMinutes"])

    def test_bed_quantities_default_to_one_and_deduplicate(self):
        result = self._settings({
            "bedTypes": [{"bedName": "Double bed", "linenCost": 75}],
            "cleaningCategories": [{
                "categoryName": "Double", "occupancy": 1,
                "beds": [
                    {"bedName": "Double bed"},
                    {"bedName": "double bed", "quantity": 4},
                    {"bedName": "  "},
                ],
            }],
        })

        self.assertEqual(
            result["cleaningCategories"][0]["beds"],
            [{"bedName": "Double bed", "quantity": 1}],
        )


class CleaningInheritanceTests(unittest.TestCase):
    """The lowest occupancy in a category carries the setup; the rows above it
    inherit beds unless overridden, and minutes whenever they are blank."""

    BEDS = [
        {"bedName": "Double bed", "linenCost": "75"},
        {"bedName": "Extra bed", "linenCost": "40"},
    ]

    def _rows(self, *rows):
        return cost_settings_service._resolve_cleaning_inheritance(
            list(rows), self.BEDS
        )

    def test_higher_occupancies_inherit_the_lowest_ones_beds_and_minutes(self):
        one, two, three = self._rows(
            {"categoryName": "Double", "occupancy": 1, "cleaningMinutes": "30",
             "linenCost": "0", "overridesBase": False,
             "beds": [{"bedName": "Double bed", "quantity": 1}]},
            {"categoryName": "Double", "occupancy": 2, "cleaningMinutes": None,
             "linenCost": "0", "overridesBase": False, "beds": []},
            {"categoryName": "Double", "occupancy": 3, "cleaningMinutes": "45",
             "linenCost": "0", "overridesBase": False, "beds": []},
        )

        self.assertTrue(one["isBase"])
        self.assertEqual(one["effectiveCleaningMinutes"], "30")
        self.assertEqual(one["effectiveLinenCost"], "75")

        # Nothing typed: takes both from the lowest occupancy.
        self.assertTrue(two["inheritsBeds"])
        self.assertTrue(two["inheritsMinutes"])
        self.assertEqual(two["effectiveCleaningMinutes"], "30")
        self.assertEqual(two["effectiveLinenCost"], "75")

        # Its own minutes, still the inherited beds. The two rules are
        # independent on purpose.
        self.assertFalse(three["inheritsMinutes"])
        self.assertTrue(three["inheritsBeds"])
        self.assertEqual(three["effectiveCleaningMinutes"], "45")
        self.assertEqual(three["effectiveLinenCost"], "75")

    def test_an_overridden_occupancy_uses_its_own_beds(self):
        _, three = self._rows(
            {"categoryName": "Double", "occupancy": 1, "cleaningMinutes": "30",
             "linenCost": "0", "overridesBase": False,
             "beds": [{"bedName": "Double bed", "quantity": 1}]},
            {"categoryName": "Double", "occupancy": 3, "cleaningMinutes": None,
             "linenCost": "0", "overridesBase": True,
             "beds": [{"bedName": "Double bed", "quantity": 1},
                      {"bedName": "Extra bed", "quantity": 2}]},
        )

        self.assertFalse(three["inheritsBeds"])
        # 75 + 2 x 40. Quantity multiplies, and the linen cost is the beds'.
        self.assertEqual(three["effectiveLinenCost"], "155")
        # Minutes still inherit: overriding the beds says nothing about time.
        self.assertTrue(three["inheritsMinutes"])
        self.assertEqual(three["effectiveCleaningMinutes"], "30")

    def test_a_row_with_no_beds_keeps_its_own_pre_bed_types_linen_cost(self):
        # Every property is in this state on the day bed types ship, so this is
        # the case that decides whether the release re-costs the estate. Each
        # row keeps exactly the figure it was costed at before: inheritance
        # governs the bed-derived cost, not the legacy one. Taking the base's
        # figure here would quietly turn 55/90 into 55/55.
        base, second = self._rows(
            {"categoryName": "Single", "occupancy": 1, "cleaningMinutes": "20",
             "linenCost": "55", "overridesBase": False, "beds": []},
            {"categoryName": "Single", "occupancy": 2, "cleaningMinutes": None,
             "linenCost": "90", "overridesBase": False, "beds": []},
        )

        self.assertEqual(base["effectiveLinenCost"], "55")
        self.assertEqual(second["effectiveLinenCost"], "90")
        # Minutes are a different question: those really were blank, so they do
        # inherit. Existing rows all carry a number, so nothing changes there
        # either until someone clears a box.
        self.assertEqual(second["effectiveCleaningMinutes"], "20")

    def test_beds_on_the_base_row_drive_every_inheriting_row(self):
        _, second = self._rows(
            {"categoryName": "Single", "occupancy": 1, "cleaningMinutes": "20",
             "linenCost": "55", "overridesBase": False,
             "beds": [{"bedName": "Double bed", "quantity": 1}]},
            {"categoryName": "Single", "occupancy": 2, "cleaningMinutes": None,
             "linenCost": "90", "overridesBase": False, "beds": []},
        )

        # Once the base has beds the inheriting row follows them, and its own
        # stale legacy figure stops being used.
        self.assertEqual(second["effectiveLinenCost"], "75")

    def test_each_category_has_its_own_base(self):
        rows = self._rows(
            {"categoryName": "Suite", "occupancy": 2, "cleaningMinutes": "60",
             "linenCost": "0", "overridesBase": False,
             "beds": [{"bedName": "Double bed", "quantity": 2}]},
            {"categoryName": "Double", "occupancy": 1, "cleaningMinutes": "30",
             "linenCost": "0", "overridesBase": False,
             "beds": [{"bedName": "Double bed", "quantity": 1}]},
            {"categoryName": "Suite", "occupancy": 4, "cleaningMinutes": None,
             "linenCost": "0", "overridesBase": False, "beds": []},
        )
        suite_four = rows[2]

        # The Suite's base is its own occupancy 2, not the Double's occupancy 1.
        self.assertEqual(suite_four["effectiveCleaningMinutes"], "60")
        self.assertEqual(suite_four["effectiveLinenCost"], "150")

    def test_the_base_row_falls_back_to_zero_rather_than_inheriting_upwards(self):
        base, = self._rows(
            {"categoryName": "Double", "occupancy": 1, "cleaningMinutes": None,
             "linenCost": "0", "overridesBase": False, "beds": []},
        )

        self.assertTrue(base["isBase"])
        self.assertFalse(base["inheritsMinutes"])
        self.assertEqual(base["effectiveCleaningMinutes"], "0")

    def test_an_unknown_bed_contributes_nothing_rather_than_crashing(self):
        # Validation rejects these on the way in, but a row saved before a bed
        # type was renamed by hand in the database must not take the dashboard
        # down with it.
        base, = self._rows(
            {"categoryName": "Double", "occupancy": 1, "cleaningMinutes": "30",
             "linenCost": "0", "overridesBase": False,
             "beds": [{"bedName": "Waterbed", "quantity": 1}]},
        )

        self.assertEqual(base["effectiveLinenCost"], "0")


if __name__ == "__main__":
    unittest.main()
