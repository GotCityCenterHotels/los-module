"""The LOS date rollup.

A year of facts at day grain is ~170k rows that the browser immediately reduces
to ~430 at month grain. Every expensive term on the request - psycopg
materialisation, the reshape loop, json.dumps, gzip, the wire, JSON.parse -
scaled with the rows nobody looked at. These tests pin the two things that make
moving the reduction into SQL safe: the server has to land rows on exactly the
period key the browser would have derived, and the grain has to reach both the
validator and the cache key.
"""

import gzip
import json
import os
import unittest

from datetime import date, datetime, timezone
from unittest.mock import patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

import function_app

from cost_database import cost_pool
from database import pool
from services import los_facts_service
from services.los_facts_service import LosFacts, LosPublication


cost_pool.close()
pool.close()


PUBLISHED_AT = datetime(2026, 8, 17, 0, 20, tzinfo=timezone.utc)
PUBLICATION = LosPublication(42, PUBLISHED_AT)


class FakeRequest:
    def __init__(self, params=None, headers=None):
        self.params = params or {}
        self.headers = headers or {}


def facts_request(grain=None, headers=None, **overrides):
    params = {
        "startDate": "2026-01-01",
        "endDate": "2026-12-31",
        "lyComparisonBasis": "sameDate",
    }
    if grain is not None:
        params["grain"] = grain
    params.update(overrides)
    return FakeRequest(params, headers)


def decode(response):
    body = response.get_body()
    if response.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return json.loads(body)


def fact(arrival_date, los=2, bookings=1, nights=2, enterprise="ent-1"):
    return {
        "arrivalDate": arrival_date,
        "hotelName": "Hotel A",
        "enterpriseId": enterprise,
        "scenario": "current",
        "los": los,
        "bookingCount": bookings,
        "nightCount": nights,
    }


class PeriodStartTests(unittest.TestCase):
    """_period_start has to agree with LosData.getPeriodKey exactly.

    If the two disagree the browser re-buckets a rolled-up row under a different
    key than the server used, and the totals silently split in two.
    """

    def test_day_grain_is_the_date_itself(self):
        self.assertEqual(
            los_facts_service._period_start(date(2026, 3, 18), "day"),
            date(2026, 3, 18),
        )

    def test_month_grain_is_the_first_of_the_month(self):
        self.assertEqual(
            los_facts_service._period_start(date(2026, 3, 18), "month"),
            date(2026, 3, 1),
        )

    def test_year_grain_is_the_first_of_january(self):
        self.assertEqual(
            los_facts_service._period_start(date(2026, 3, 18), "year"),
            date(2026, 1, 1),
        )

    def test_week_grain_is_the_monday(self):
        # 2026-03-18 is a Wednesday; the browser's (getUTCDay() + 6) % 7 and
        # Python's weekday() both put Monday at 0.
        self.assertEqual(
            los_facts_service._period_start(date(2026, 3, 18), "week"),
            date(2026, 3, 16),
        )

    def test_a_monday_is_its_own_week_start(self):
        self.assertEqual(
            los_facts_service._period_start(date(2026, 3, 16), "week"),
            date(2026, 3, 16),
        )

    def test_a_sunday_belongs_to_the_week_that_began_six_days_earlier(self):
        self.assertEqual(
            los_facts_service._period_start(date(2026, 3, 22), "week"),
            date(2026, 3, 16),
        )

    def test_an_unsupported_grain_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            los_facts_service._period_start(date(2026, 3, 18), "quarter")


class RollupFactsTests(unittest.TestCase):
    """The fallback rollup must be additive and must not lose a dimension."""

    def test_day_grain_returns_the_rows_untouched(self):
        rows = [fact("2026-03-18")]
        self.assertIs(los_facts_service._rollup_facts(rows, "day"), rows)

    def test_rows_in_one_month_collapse_and_their_counts_add(self):
        rolled = los_facts_service._rollup_facts(
            [
                fact("2026-03-01", bookings=3, nights=6),
                fact("2026-03-18", bookings=2, nights=4),
            ],
            "month",
        )
        self.assertEqual(len(rolled), 1)
        self.assertEqual(rolled[0]["arrivalDate"], "2026-03-01")
        self.assertEqual(rolled[0]["bookingCount"], 5)
        self.assertEqual(rolled[0]["nightCount"], 10)

    def test_the_average_survives_the_rollup_exactly(self):
        # averageLos is sum(nights)/sum(bookings), so summing both preserves it.
        rolled = los_facts_service._rollup_facts(
            [
                fact("2026-03-01", los=2, bookings=3, nights=6),
                fact("2026-03-18", los=5, bookings=1, nights=5),
            ],
            "year",
        )
        total_nights = sum(row["nightCount"] for row in rolled)
        total_bookings = sum(row["bookingCount"] for row in rolled)
        self.assertEqual(total_nights / total_bookings, 11 / 4)

    def test_different_los_values_stay_separate(self):
        # The Distribution page buckets by los, so collapsing it would break it.
        rolled = los_facts_service._rollup_facts(
            [fact("2026-03-01", los=2), fact("2026-03-18", los=5)],
            "month",
        )
        self.assertEqual(len(rolled), 2)
        self.assertEqual({row["los"] for row in rolled}, {2, 5})

    def test_different_hotels_stay_separate(self):
        # Both pages filter by hotel in the browser.
        rolled = los_facts_service._rollup_facts(
            [
                fact("2026-03-01", enterprise="ent-1"),
                fact("2026-03-18", enterprise="ent-2"),
            ],
            "month",
        )
        self.assertEqual(len(rolled), 2)

    def test_separate_months_do_not_merge(self):
        rolled = los_facts_service._rollup_facts(
            [fact("2026-03-18"), fact("2026-04-18")],
            "month",
        )
        self.assertEqual(
            sorted(row["arrivalDate"] for row in rolled),
            ["2026-03-01", "2026-04-01"],
        )

    def test_a_week_spanning_a_month_boundary_stays_one_bucket(self):
        # This is the case that makes the browser's month filter unusable on
        # rolled-up data, and why the request range carries the month selection
        # instead: the bucket key can fall outside the months it covers.
        rolled = los_facts_service._rollup_facts(
            [fact("2026-04-01"), fact("2026-04-02")],
            "week",
        )
        self.assertEqual(len(rolled), 1)
        self.assertEqual(rolled[0]["arrivalDate"], "2026-03-30")


class GrainParameterTests(unittest.TestCase):
    def setUp(self):
        function_app._los_facts_response_bytes.entries.clear()
        function_app._los_facts_response_bytes.inflight.clear()

    tearDown = setUp

    def published(self, rows=None, publication=PUBLICATION):
        rows = rows if rows is not None else [fact("2026-01-01")]
        return (
            patch.object(
                function_app, "los_read_model_enabled", return_value=True
            ),
            patch.object(
                function_app, "fetch_los_publication", return_value=publication
            ),
            patch.object(
                function_app,
                "fetch_los_facts",
                return_value=LosFacts(
                    rows, publication.run_id, publication.published_at
                ),
            ),
        )

    def test_an_absent_grain_defaults_to_day(self):
        # A cached client that predates the parameter has to keep working.
        enabled, publication, facts = self.published()
        with enabled, publication, facts as fetch:
            response = function_app.los_facts(facts_request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(decode(response)["parameters"]["grain"], "day")
        self.assertEqual(fetch.call_args.args[4], "day")

    def test_each_supported_grain_reaches_the_service(self):
        for grain in ("day", "week", "month", "year"):
            with self.subTest(grain=grain):
                self.setUp()
                enabled, publication, facts = self.published()
                with enabled, publication, facts as fetch:
                    response = function_app.los_facts(facts_request(grain))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(fetch.call_args.args[4], grain)
                self.assertEqual(
                    decode(response)["parameters"]["grain"], grain
                )

    def test_an_unknown_grain_is_refused(self):
        response = function_app.los_facts(facts_request("quarter"))
        self.assertEqual(response.status_code, 400)
        payload = json.loads(response.get_body())
        self.assertEqual(payload["error"], "Invalid grain")
        self.assertIn("month", payload["allowedValues"])

    def test_two_grains_are_two_validators(self):
        enabled, publication, facts = self.published()
        with enabled, publication, facts:
            month = function_app.los_facts(facts_request("month"))
        self.setUp()
        enabled, publication, facts = self.published()
        with enabled, publication, facts:
            year = function_app.los_facts(facts_request("year"))

        self.assertNotEqual(month.headers["ETag"], year.headers["ETag"])

    def test_a_second_grain_is_not_served_from_the_first_grains_bytes(self):
        """The grain has to be in the cache key, not only in the ETag.

        Keyed on the range alone, asking for year after month would return the
        month body under the year ETag - wrong rows, advertised as current.
        """
        enabled, publication, facts = self.published(
            rows=[fact("2026-01-01", bookings=1, nights=2)]
        )
        with enabled, publication, facts:
            function_app.los_facts(facts_request("month"))

        year_rows = [fact("2026-01-01", bookings=99, nights=198)]
        enabled, publication, facts = self.published(rows=year_rows)
        with enabled, publication, facts:
            year = function_app.los_facts(facts_request("year"))

        self.assertEqual(decode(year)["data"], year_rows)

    def test_the_same_grain_twice_reuses_the_built_bytes(self):
        enabled, publication, facts = self.published()
        with enabled, publication, facts as fetch:
            function_app.los_facts(facts_request("month"))
            function_app.los_facts(facts_request("month"))

        self.assertEqual(fetch.call_count, 1)

    def test_the_raw_fallback_also_honours_the_grain(self):
        # One response contract whichever path answered, so the browser does not
        # have to know which one did.
        with patch.object(
            function_app, "los_read_model_enabled", return_value=False
        ), patch.object(
            function_app,
            "fetch_los_facts",
            return_value=LosFacts([fact("2026-01-01")], None, None),
        ) as fetch:
            response = function_app.los_facts(facts_request("month"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(decode(response)["parameters"]["grain"], "month")
        self.assertEqual(fetch.call_args.args[4], "month")


if __name__ == "__main__":
    unittest.main()
