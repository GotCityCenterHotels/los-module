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
        self.assertIn("order by enterprise_id, sort_order, category_name", cleaning)


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


if __name__ == "__main__":
    unittest.main()
