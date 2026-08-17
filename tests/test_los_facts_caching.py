import gzip
import json
import os
import unittest

from datetime import datetime, timezone
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
from services.los_facts_service import LosFacts


cost_pool.close()
pool.close()


PUBLISHED_AT = datetime(2026, 8, 17, 0, 20, tzinfo=timezone.utc)
ROWS = [
    {
        "arrivalDate": "2026-08-01",
        "hotelCode": "Hotel A",
        "enterpriseId": "ent-1",
        "hotelName": "Hotel A",
        "scenario": "current",
        "los": 2,
        "bookingCount": 3,
        "nightCount": 6,
    }
]


class FakeRequest:
    def __init__(self, params=None, headers=None):
        self.params = params or {}
        self.headers = headers or {}


def facts_request(headers=None):
    return FakeRequest(
        {
            "startDate": "2026-08-01",
            "endDate": "2026-08-31",
            "lyComparisonBasis": "sameDate",
        },
        headers,
    )


def decode(response):
    body = response.get_body()
    if response.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return json.loads(body)


class LosFactsCachingTests(unittest.TestCase):
    """Average LOS and LOS Distribution read the same published facts.

    Opening one after the other, or reopening either, repeats a request whose
    answer cannot have changed while the publication has not - so the response
    carries a validator the browser can answer with by itself.
    """

    def test_published_facts_carry_a_publication_derived_validator(self):
        with patch.object(
            function_app,
            "fetch_los_facts",
            return_value=LosFacts(ROWS, 42, PUBLISHED_AT),
        ):
            response = function_app.los_facts(facts_request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "private, max-age=300")
        self.assertTrue(response.headers["ETag"].startswith('W/"los-facts-'))
        payload = decode(response)
        self.assertEqual(payload["rowCount"], 1)
        self.assertEqual(payload["runId"], 42)
        self.assertEqual(payload["data"], ROWS)

    def test_an_unchanged_publication_answers_a_repeat_with_304(self):
        with patch.object(
            function_app,
            "fetch_los_facts",
            return_value=LosFacts(ROWS, 42, PUBLISHED_AT),
        ):
            first = function_app.los_facts(facts_request())
            repeat = function_app.los_facts(
                facts_request({"If-None-Match": first.headers["ETag"]})
            )

        self.assertEqual(repeat.status_code, 304)
        self.assertEqual(repeat.headers["ETag"], first.headers["ETag"])

    def test_a_new_publication_invalidates_the_previous_validator(self):
        with patch.object(
            function_app,
            "fetch_los_facts",
            return_value=LosFacts(ROWS, 42, PUBLISHED_AT),
        ):
            before = function_app.los_facts(facts_request())
        with patch.object(
            function_app,
            "fetch_los_facts",
            return_value=LosFacts(ROWS, 43, PUBLISHED_AT),
        ):
            after = function_app.los_facts(facts_request())

        self.assertNotEqual(before.headers["ETag"], after.headers["ETag"])

    def test_a_different_range_is_a_different_validator(self):
        with patch.object(
            function_app,
            "fetch_los_facts",
            return_value=LosFacts(ROWS, 42, PUBLISHED_AT),
        ):
            august = function_app.los_facts(facts_request())
            september = function_app.los_facts(
                FakeRequest({
                    "startDate": "2026-09-01",
                    "endDate": "2026-09-30",
                    "lyComparisonBasis": "sameDate",
                })
            )

        self.assertNotEqual(august.headers["ETag"], september.headers["ETag"])

    def test_the_raw_query_fallback_is_never_cached(self):
        # Without the read model the rows come from live source data, so there
        # is no publication to say the answer has not moved.
        with patch.object(
            function_app,
            "fetch_los_facts",
            return_value=LosFacts(ROWS, None, None),
        ):
            response = function_app.los_facts(facts_request())

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("Cache-Control"))
        self.assertIsNone(response.headers.get("ETag"))
        self.assertIsNone(decode(response)["runId"])


if __name__ == "__main__":
    unittest.main()
