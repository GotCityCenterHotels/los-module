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
            "cleaningCategories": [{"categoryName": "Double", "minGuests": 1, "maxGuests": 2, "cleaningMinutes": 30, "linenCost": 75}],
            "arrivalTiers": [{"minArrivals": 30, "maxArrivals": "", "receptionHours": 4}],
            "breakfastTiers": [{"minGuests": 0, "maxGuests": 49, "staffHours": 0}, {"minGuests": 50, "maxGuests": 70, "staffHours": 4}],
            "fixedCosts": [{"costName": "Electricity", "amount": 10000, "cadence": "monthly", "active": True}],
        })

        self.assertEqual(result["hotelName"], "Hotel A")
        self.assertEqual(result["profile"]["currency"], "SEK")
        self.assertEqual(result["arrivalTiers"][0]["maxArrivals"], None)

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
