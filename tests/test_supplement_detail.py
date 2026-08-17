"""End-to-end cover for the Supplement detail path.

Every query this endpoint issues is answered by a fake cursor that dispatches on
the SQL, so the whole function runs - validation, the read-model reads, the
assembly - without a database. The path had no such cover, which is how a
refactor of its queries reached production as a 500.
"""

import gzip
import json
import os
import unittest

from datetime import date, datetime, timezone
from unittest.mock import patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "integration_db")
os.environ.setdefault("DB_USER", "readonly")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("INTEGRATION_DB_HOST", "localhost")
os.environ.setdefault("INTEGRATION_DB_NAME", "integration_db")
os.environ.setdefault("INTEGRATION_DB_USER", "readonly")
os.environ.setdefault("INTEGRATION_DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

import function_app

from cost_database import cost_pool
from database import pool
from services import supplement_service


# These tests never reach a database; closing the pools keeps their reconnect
# threads from running against the placeholder credentials above, the way every
# other module here does.
cost_pool.close()
pool.close()


HOTEL = "ent-1"
CATEGORY = "11111111-1111-1111-1111-111111111111"
STAY_DATE = date(2026, 8, 17)
COMPARISON_DATE = date(2025, 8, 17)
DATA_AS_OF = date(2026, 8, 16)


class FakeCursor:
    def __init__(self, answer):
        self.answer = answer
        self.statements = []
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        return False

    def execute(self, sql, parameters=None):
        self.statements.append((" ".join(sql.split()), parameters))
        self._rows = self.answer(sql, parameters)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        return False

    def cursor(self, *arguments, **keywords):
        return self._cursor


def answer(sql, parameters):
    """One plausible result set per query the detail path issues."""
    if "supplement_publication" in sql:
        return [{
            "run_id": 7,
            "data_as_of": DATA_AS_OF,
            "published_at": datetime.now(timezone.utc),
            "minimum_stay_date": date(2024, 1, 1),
            "maximum_stay_date": date(2027, 12, 31),
            "minimum_snapshot_date": date(2024, 1, 1),
            "maximum_snapshot_date": DATA_AS_OF,
        }]
    if "FROM functions.hotels" in sql:
        return [{"hotel_code": HOTEL, "hotel_name": "Hotel Årsta"}]
    if "supplement_room_categories" in sql:
        return [{
            "hotel_code": HOTEL,
            "room_category_code": CATEGORY,
            "space_room_name": "Standard Double",
            "short_name": "STD",
            "sort_order": 1,
        }]
    if "supplement_latest_detail" in sql or "supplement_snapshot_detail" in sql:
        return [{
            "requested_room_category_code": "22222222-2222-2222-2222-222222222222",
            "requested_room_name": "Double room",
            "assigned_rooms": 12,
            "room_revenue": 18000,
        }]
    if "supplement_latest_inventory" in sql:
        return [
            {
                "stay_date": stay_date,
                "total_space": 30,
                "space_to_sell": 25,
                "inventory_quality": "exact",
            }
            for stay_date in parameters["stay_dates"]
        ]
    if "supplement_snapshot_inventory" in sql:
        return [{
            "stay_date": STAY_DATE,
            "snapshot_date": date(2026, 8, 10),
            "total_space": 30,
            "space_to_sell": 25,
            "inventory_quality": "exact",
        }]
    raise AssertionError(f"unexpected query: {' '.join(sql.split())[:120]}")


PICKUP_HISTORY = [
    {"snapshot_date": date(2026, 8, 10), "assigned_rooms": 8, "room_revenue": 9600},
    {"snapshot_date": date(2026, 8, 15), "assigned_rooms": 12, "room_revenue": 18000},
]

# The booking-mix query for a future stay names supplement_snapshot_inventory in
# its CTE, so the inventory reads are identified by their grouping instead.
LATEST_INVENTORY_READ = "FROM functions.supplement_latest_inventory WHERE"
STORED_INVENTORY_READ = "GROUP BY i.stay_date, i.snapshot_date"


class SupplementDetailTests(unittest.TestCase):
    def setUp(self):
        supplement_service._detail_cache.clear()
        supplement_service._summary_cache.clear()
        supplement_service._metadata_cache.clear()
        self.cursor = FakeCursor(answer)

    def tearDown(self):
        supplement_service._detail_cache.clear()
        supplement_service._summary_cache.clear()
        supplement_service._metadata_cache.clear()

    def _count(self, fragment):
        return sum(fragment in sql for sql, _ in self.cursor.statements)

    def _fetch(self, **keywords):
        with patch.object(
            supplement_service, "ensure_supplement_schema"
        ), patch.object(
            supplement_service, "stockholm_today", return_value=date(2026, 8, 13)
        ), patch.object(
            supplement_service.cost_pool,
            "connection",
            return_value=FakeConnection(self.cursor),
        ), patch.object(
            supplement_service,
            "fetch_pickup_history",
            return_value=list(PICKUP_HISTORY),
        ) as history:
            payload = supplement_service.fetch_supplement_detail(
                HOTEL, STAY_DATE, CATEGORY, "sameDate", **keywords
            )
        self.history_calls = history.call_count
        return payload

    def test_full_detail_assembles_figures_and_both_curves(self):
        payload = self._fetch()

        self.assertEqual(payload["hotelCode"], HOTEL)
        self.assertEqual(payload["stayDate"], STAY_DATE.isoformat())
        self.assertEqual(payload["totalAssignedRooms"], 12)
        self.assertEqual(payload["totalAveragePrice"], 18000 / 12)
        # Sellable basis, so occupancy divides by space_to_sell.
        self.assertEqual(payload["inventory"], 25)
        self.assertEqual(payload["physicalInventory"], 30)
        self.assertEqual(payload["sellableInventory"], 25)
        self.assertEqual(len(payload["breakdown"]), 1)
        self.assertEqual(len(payload["pickup"]), len(PICKUP_HISTORY))
        self.assertEqual(len(payload["comparisonPickup"]), len(PICKUP_HISTORY))
        self.assertEqual(self.history_calls, 2, "one rebuild per curve")

    def test_summary_never_touches_the_source_and_drops_the_curves(self):
        payload = self._fetch(include="summary")

        self.assertEqual(self.history_calls, 0, "the summary must not reach the source")
        for dropped in ("pickup", "comparisonPickup", "pickupHistoryDays"):
            self.assertNotIn(dropped, payload)
        self.assertEqual(payload["totalAssignedRooms"], 12)
        self.assertEqual(payload["inventory"], 25)
        self.assertEqual(len(payload["breakdown"]), 1)
        # And it skips the stored-snapshot read only the curves need. Matched on
        # the grouping rather than the table, because the snapshot booking-mix
        # query names that table too, in its CTE.
        self.assertEqual(self._count(STORED_INVENTORY_READ), 0)

    def test_the_lookback_window_slices_the_returned_curve(self):
        payload = self._fetch(days_before_stay=3)
        self.assertTrue(
            all(point["daysBeforeStay"] <= 3 for point in payload["pickup"])
        )
        self.assertEqual(payload["daysBeforeStay"], 3)

    def test_physical_basis_reports_physical_inventory(self):
        payload = self._fetch(inventory_basis="physical")
        self.assertEqual(payload["inventory"], 30)
        self.assertEqual(payload["inventoryBasis"], "physical")

    def test_all_categories_detail_needs_no_category(self):
        with patch.object(
            supplement_service, "ensure_supplement_schema"
        ), patch.object(
            supplement_service, "stockholm_today", return_value=date(2026, 8, 13)
        ), patch.object(
            supplement_service.cost_pool,
            "connection",
            return_value=FakeConnection(self.cursor),
        ), patch.object(
            supplement_service, "fetch_pickup_history", return_value=list(PICKUP_HISTORY)
        ):
            payload = supplement_service.fetch_supplement_detail(
                HOTEL, STAY_DATE, None, "sameDate"
            )
        self.assertIsNone(payload["roomCategory"])
        self.assertEqual(payload["totalAssignedRooms"], 12)

    def test_a_category_spelled_differently_is_still_the_same_category(self):
        # The removed existence query cast to uuid, which accepted any spelling
        # Postgres recognises. Losing that would turn a working cell into an
        # error over its capitalisation.
        with patch.object(
            supplement_service, "ensure_supplement_schema"
        ), patch.object(
            supplement_service, "stockholm_today", return_value=date(2026, 8, 13)
        ), patch.object(
            supplement_service.cost_pool,
            "connection",
            return_value=FakeConnection(self.cursor),
        ), patch.object(
            supplement_service, "fetch_pickup_history", return_value=list(PICKUP_HISTORY)
        ):
            payload = supplement_service.fetch_supplement_detail(
                HOTEL, STAY_DATE, CATEGORY.upper(), "sameDate"
            )
        self.assertEqual(payload["roomCategory"], CATEGORY)

    def test_unknown_hotel_and_category_are_client_errors(self):
        for arguments in (
            ("nope", STAY_DATE, CATEGORY),
            (HOTEL, STAY_DATE, "33333333-3333-3333-3333-333333333333"),
        ):
            with self.subTest(arguments=arguments):
                supplement_service._metadata_cache.clear()
                with patch.object(
                    supplement_service, "ensure_supplement_schema"
                ), patch.object(
                    supplement_service, "stockholm_today", return_value=date(2026, 8, 13)
                ), patch.object(
                    supplement_service.cost_pool,
                    "connection",
                    return_value=FakeConnection(FakeCursor(answer)),
                ), patch.object(
                    supplement_service,
                    "fetch_pickup_history",
                    side_effect=AssertionError("must not reach the source"),
                ):
                    with self.assertRaises(ValueError):
                        supplement_service.fetch_supplement_detail(
                            *arguments, "sameDate"
                        )

    def test_a_failed_rebuild_degrades_instead_of_losing_the_figures(self):
        # The curves come from the source database under a tight statement
        # ceiling; the figures beside them are already in hand. Losing the whole
        # response over the slow half threw away the fast half too.
        with patch.object(
            supplement_service, "ensure_supplement_schema"
        ), patch.object(
            supplement_service, "stockholm_today", return_value=date(2026, 8, 13)
        ), patch.object(
            supplement_service.cost_pool,
            "connection",
            return_value=FakeConnection(self.cursor),
        ), patch.object(
            supplement_service,
            "fetch_pickup_history",
            side_effect=RuntimeError("canceling statement due to statement timeout"),
        ):
            payload = supplement_service.fetch_supplement_detail(
                HOTEL, STAY_DATE, CATEGORY, "sameDate"
            )

        self.assertFalse(payload["pickupAvailable"])
        self.assertIn("could not be rebuilt", payload["pickupUnavailableReason"])
        self.assertEqual(payload["pickup"], [])
        # The published figures still went out.
        self.assertEqual(payload["totalAssignedRooms"], 12)
        self.assertEqual(payload["inventory"], 25)
        self.assertEqual(len(payload["breakdown"]), 1)

    def test_a_failed_rebuild_is_not_cached(self):
        # Otherwise one bad minute leaves the curve missing until the next
        # publication.
        with patch.object(
            supplement_service, "ensure_supplement_schema"
        ), patch.object(
            supplement_service, "stockholm_today", return_value=date(2026, 8, 13)
        ), patch.object(
            supplement_service.cost_pool,
            "connection",
            return_value=FakeConnection(self.cursor),
        ), patch.object(
            supplement_service, "fetch_pickup_history", side_effect=RuntimeError("boom")
        ):
            supplement_service.fetch_supplement_detail(
                HOTEL, STAY_DATE, CATEGORY, "sameDate"
            )
        self.assertEqual(len(supplement_service._detail_cache), 0)

        recovered = self._fetch()
        self.assertTrue(recovered["pickupAvailable"])
        self.assertEqual(len(recovered["pickup"]), len(PICKUP_HISTORY))

    def test_a_healthy_rebuild_is_marked_available(self):
        self.assertTrue(self._fetch()["pickupAvailable"])

    def test_read_model_round_trips_stay_collapsed(self):
        self._fetch()
        reads = [sql for sql, _ in self.cursor.statements]
        # publication, hotels, categories, two booking-mix reads, one latest
        # inventory, one stored inventory. The hotel and category existence
        # checks are answered from the cached dimensions, not from the database.
        self.assertEqual(len(reads), 7, "\n".join(reads))
        self.assertEqual(
            self._count(LATEST_INVENTORY_READ), 1,
            "the summary figure and both curve fallbacks share one read",
        )
        self.assertEqual(
            self._count(STORED_INVENTORY_READ), 1,
            "both curves' stored inventory shares one read",
        )
        self.assertFalse(
            any("SELECT 1 FROM functions.hotels" in sql for sql in reads),
            "the hotel check should come from the cached dimensions",
        )


class FakeRequest:
    def __init__(self, params, headers=None):
        self.params = params
        self.headers = headers or {}


class SupplementDetailRouteTests(unittest.TestCase):
    """The route, not just the service.

    The endpoint answers a failure with a flat "Unable to retrieve Supplement
    detail", so anything the service returns that will not serialise, or any
    wiring mistake between the two, surfaces to a user as that one sentence and
    to nobody else as anything. These run the whole way through.
    """

    def setUp(self):
        supplement_service._detail_cache.clear()
        supplement_service._summary_cache.clear()
        supplement_service._metadata_cache.clear()

    tearDown = setUp

    def _call(self, extra=None):
        params = {
            "hotelCode": HOTEL,
            "stayDate": STAY_DATE.isoformat(),
            "roomCategory": CATEGORY,
            "lyComparisonBasis": "sameDate",
            "inventoryBasis": "sellable",
        }
        params.update(extra or {})
        with patch.dict(os.environ, {"SUPPLEMENT_LIVE_ENABLED": "true"}), patch.object(
            supplement_service, "ensure_supplement_schema"
        ), patch.object(
            supplement_service, "stockholm_today", return_value=date(2026, 8, 13)
        ), patch.object(
            supplement_service.cost_pool,
            "connection",
            return_value=FakeConnection(FakeCursor(answer)),
        ), patch.object(
            supplement_service, "fetch_pickup_history", return_value=list(PICKUP_HISTORY)
        ):
            return function_app.supplement_detail(FakeRequest(params))

    def _body(self, response):
        body = response.get_body()
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return json.loads(body)

    def test_full_detail_serialises_and_returns_200(self):
        response = self._call({"daysBeforeStay": "all"})
        self.assertEqual(response.status_code, 200, response.get_body()[:400])
        payload = self._body(response)
        self.assertEqual(payload["hotelCode"], HOTEL)
        self.assertEqual(len(payload["pickup"]), len(PICKUP_HISTORY))

    def test_summary_serialises_and_returns_200(self):
        response = self._call({"include": "summary"})
        self.assertEqual(response.status_code, 200, response.get_body()[:400])
        payload = self._body(response)
        self.assertNotIn("pickup", payload)
        self.assertEqual(payload["totalAssignedRooms"], 12)

    def test_a_windowed_request_serialises(self):
        response = self._call({"daysBeforeStay": "30"})
        self.assertEqual(response.status_code, 200, response.get_body()[:400])

    def test_the_two_halves_get_different_validators(self):
        full = self._call({"daysBeforeStay": "all"})
        summary = self._call({"include": "summary"})
        self.assertNotEqual(full.headers["ETag"], summary.headers["ETag"])

    def test_an_unknown_include_is_a_client_error_not_a_500(self):
        response = self._call({"include": "curves"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("include", self._body(response)["error"])


if __name__ == "__main__":
    unittest.main()
