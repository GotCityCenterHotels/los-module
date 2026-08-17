import os
import unittest

from datetime import date
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "integration_db")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from services import supplement_service
from services import supplement_sync_service


class SupplementDomainTests(unittest.TestCase):
    def test_same_weekday_uses_364_day_module_convention(self):
        source = date(2026, 8, 13)
        shifted = supplement_service.shift_last_year(source, "sameWeekday")
        self.assertEqual(shifted, date(2025, 8, 14))
        self.assertEqual(source.weekday(), shifted.weekday())

    def test_same_date_clamps_leap_day(self):
        self.assertEqual(
            supplement_service.shift_last_year(date(2024, 2, 29), "sameDate"),
            date(2023, 2, 28),
        )

    def test_grid_range_is_limited_to_366_days(self):
        self.assertEqual(
            supplement_service.validate_date_range(date(2024, 1, 1), date(2024, 12, 31)),
            366,
        )
        with self.assertRaisesRegex(ValueError, "366 days"):
            supplement_service.validate_date_range(date(2024, 1, 1), date(2025, 1, 1))

    def test_metrics_are_weighted_from_additive_facts(self):
        metrics = supplement_service.calculate_metrics(50, 100000, 80)
        self.assertEqual(metrics["occ"], 62.5)
        self.assertEqual(metrics["adr"], 2000)
        self.assertEqual(metrics["revpar"], 1250)

    def test_inventory_basis_changes_occ_and_revpar_not_adr(self):
        sellable = supplement_service.calculate_metrics(50, 100000, 100, 80)
        physical = supplement_service.calculate_metrics(
            50, 100000, 100, 80, "physical", "approximated-current"
        )
        self.assertEqual(sellable["occ"], 62.5)
        self.assertEqual(physical["occ"], 50)
        self.assertEqual(sellable["adr"], physical["adr"])
        self.assertEqual(physical["physicalInventory"], 100)
        self.assertEqual(physical["sellableInventory"], 80)
        self.assertEqual(physical["inventoryQuality"], "approximated-current")

    def test_invalid_inventory_basis_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "inventoryBasis"):
            supplement_service.calculate_metrics(1, 1, 1, 1, "rooms")

    def test_sync_horizon_adds_eighteen_months(self):
        self.assertEqual(
            supplement_sync_service.add_months(date(2024, 8, 31), 18),
            date(2026, 2, 28),
        )

    def test_summary_view_drops_the_curves_and_keeps_every_figure(self):
        # The figures come from the published read model and are ready in
        # milliseconds; the curves are rebuilt from the source and are the slow
        # half. The summary view is what lets the dialog paint the first without
        # waiting for the second, so it must carry everything above the chart.
        payload = {
            "runId": 7, "dataAsOf": "2026-08-16", "hotelCode": "ent-1",
            "stayDate": "2026-08-17", "roomCategory": None, "comparison": "SPIT",
            "totalAssignedRooms": 12, "totalAveragePrice": 1200.0,
            "inventory": 20, "inventoryBasis": "sellable",
            "inventoryQuality": "exact", "comparisonAvailable": True,
            "breakdown": [{"requestedRoomName": "Double"}],
            "pickup": [{"daysBeforeStay": 3}],
            "comparisonPickup": [{"daysBeforeStay": 3}],
            "pickupHistoryDays": 399, "daysBeforeStay": None,
        }
        view = supplement_service._summary_view(payload)

        for dropped in ("pickup", "comparisonPickup", "pickupHistoryDays", "daysBeforeStay"):
            self.assertNotIn(dropped, view)
        for kept in (
            "runId", "dataAsOf", "hotelCode", "stayDate", "comparison",
            "totalAssignedRooms", "totalAveragePrice", "inventory",
            "inventoryBasis", "inventoryQuality", "comparisonAvailable", "breakdown",
        ):
            self.assertEqual(view[kept], payload[kept], kept)
        # And it is a copy: projecting must not empty the cached payload.
        self.assertEqual(len(payload["pickup"]), 1)

    def test_unknown_include_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "include"):
            supplement_service.fetch_supplement_detail(
                "Hotel A", date(2026, 8, 1), None, "sameDate", include="curves"
            )

    def test_all_category_detail_does_not_bind_an_untyped_null(self):
        source = Path(supplement_service.__file__).read_text(encoding="utf-8")
        self.assertNotIn("(%s IS NULL OR space_room_category_id", source)
        self.assertIn("inventory_category_clause", source)

    def test_missing_comparison_coverage_does_not_hide_current_detail(self):
        # The coverage guard bounds the requested stay date and nothing else. A
        # last-year date outside coverage must leave the current figures intact
        # and be reported through comparisonAvailable instead of failing the
        # whole request.
        #
        # Anchored on the guard's own raise rather than on whatever statement
        # happens to follow it, so reorganising the queries around it does not
        # break the check.
        source = Path(supplement_service.__file__).read_text(encoding="utf-8")
        detail_source = source[source.index("def fetch_supplement_detail"):]
        start = detail_source.index("if coverage and (")
        coverage_guard = detail_source[start:detail_source.index(
            "The selected detail date has not been backfilled", start
        )]
        self.assertNotIn("comparison_date", coverage_guard)
        self.assertIn('"comparisonAvailable"', detail_source)


class SupplementApiBoundaryTests(unittest.TestCase):
    def test_all_read_services_use_only_database_a_pool(self):
        # Both doors onto integration_db are held shut: the one-off connection
        # the sync paths use, and the pool the interactive pickup path uses.
        # Database A has to answer first - publication, coverage, hotel and
        # category - so an unknown identifier never reaches the source at all.
        calls = (
            supplement_service.fetch_supplement_status,
            supplement_service.list_supplement_hotels,
            lambda: supplement_service.fetch_supplement_grid(
                date(2026, 8, 1), date(2026, 8, 7)
            ),
            lambda: supplement_service.fetch_supplement_detail(
                "Hotel A", date(2026, 8, 1), None, "sameDate"
            ),
        )
        for call in calls:
            with self.subTest(call=call), patch.object(
                supplement_service,
                "ensure_supplement_schema",
            ), patch.object(
                supplement_service,
                "stockholm_today",
                return_value=date(2026, 8, 13),
            ), patch.object(
                supplement_service.cost_pool,
                "connection",
                side_effect=RuntimeError("database-a-probe"),
            ), patch(
                "queries.supplement_source.get_export_connection",
                side_effect=AssertionError("integration_db must not be opened"),
            ) as source, patch(
                "queries.supplement_source._pickup_connection_pool",
                side_effect=AssertionError("integration_db must not be opened"),
            ) as source_pool:
                with self.assertRaisesRegex(RuntimeError, "database-a-probe"):
                    call()
                source.assert_not_called()
                source_pool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
