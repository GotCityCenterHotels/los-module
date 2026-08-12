import os
import unittest
from unittest.mock import patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")

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
        with patch.object(cost_settings_service, "get_export_connection", return_value=connection):
            result = cost_settings_service.list_cost_settings_hotels()

        self.assertEqual(result, [{
            "enterpriseId": "00000000-0000-0000-0000-000000000001",
            "hotelName": "Hotel A",
        }])
        self.assertIn("FROM enterprise_current", connection.cursor_instance.sql)

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
            "get_export_connection",
            return_value=connection,
        ) as source_connection:
            result = cost_settings_service._get_cost_settings_hotel("property-42")

        source_connection.assert_called_once_with()
        self.assertEqual(result["hotelName"], "Hotel A")
        self.assertIn("id::text = %s", connection.cursor_instance.sql)
        self.assertEqual(connection.cursor_instance.parameters, ("property-42",))

    def test_property_list_falls_back_to_imported_cost_data(self):
        imported = [{"enterpriseId": "property-42", "hotelName": "Hotel A"}]

        with patch.object(
            cost_settings_service,
            "_list_source_properties",
            side_effect=RuntimeError("source unavailable"),
        ), patch.object(
            cost_settings_service,
            "_list_imported_properties",
            return_value=imported,
        ) as imported_properties:
            result = cost_settings_service.list_cost_settings_hotels()

        self.assertEqual(result, imported)
        imported_properties.assert_called_once_with()

    def test_property_lookup_falls_back_to_imported_cost_data(self):
        imported = {"enterpriseId": "property-42", "hotelName": "Hotel A"}

        with patch.object(
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


if __name__ == "__main__":
    unittest.main()
