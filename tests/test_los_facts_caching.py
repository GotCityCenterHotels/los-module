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
from services.los_facts_service import LosFacts, LosPublication


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


PUBLICATION = LosPublication(42, PUBLISHED_AT)


def published(rows=ROWS, publication=PUBLICATION):
    """Patch the route onto one publication and the facts it would return.

    The route resolves the publication before it decides to build anything, so a
    test that only stubbed the facts would send the identity lookup at a closed
    pool.
    """
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


class LosFactsCachingTests(unittest.TestCase):
    """Average LOS and LOS Distribution read the same published facts.

    Opening one after the other, or reopening either, repeats a request whose
    answer cannot have changed while the publication has not - so the response
    carries a validator the browser can answer with by itself.
    """

    def setUp(self):
        # A worker-local byte cache would otherwise carry one case's response
        # into the next, which is exactly what the cache is for and exactly what
        # makes tests lie to each other.
        function_app._los_facts_response_bytes.entries.clear()
        function_app._los_facts_response_bytes.inflight.clear()

    def tearDown(self):
        function_app._los_facts_response_bytes.entries.clear()
        function_app._los_facts_response_bytes.inflight.clear()

    def test_published_facts_carry_a_publication_derived_validator(self):
        enabled, publication, facts = published()
        with enabled, publication, facts:
            response = function_app.los_facts(facts_request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "private, max-age=300")
        self.assertTrue(response.headers["ETag"].startswith('W/"los-facts-'))
        payload = decode(response)
        self.assertEqual(payload["rowCount"], 1)
        self.assertEqual(payload["runId"], 42)
        self.assertEqual(payload["data"], ROWS)

    def test_an_unchanged_publication_answers_a_repeat_with_304(self):
        enabled, publication, facts = published()
        with enabled, publication, facts:
            first = function_app.los_facts(facts_request())
            repeat = function_app.los_facts(
                facts_request({"If-None-Match": first.headers["ETag"]})
            )

        self.assertEqual(repeat.status_code, 304)
        self.assertEqual(repeat.headers["ETag"], first.headers["ETag"])

    def test_a_validated_repeat_never_runs_the_fact_query(self):
        """Why the publication is resolved on its own.

        A 304 used to cost a full range scan and a full row shaping before the
        validator was even known. Now the only database work behind one is the
        single-row identity read.
        """
        enabled, publication, facts = published()
        with enabled, publication, facts as fetch:
            first = function_app.los_facts(facts_request())
            self.assertEqual(fetch.call_count, 1)
            repeat = function_app.los_facts(
                facts_request({"If-None-Match": first.headers["ETag"]})
            )

        self.assertEqual(repeat.status_code, 304)
        self.assertEqual(fetch.call_count, 1)

    def test_a_second_reader_of_the_same_range_reuses_the_built_bytes(self):
        """The other half: an unvalidated repeat is not a rebuild either.

        Average LOS and LOS Distribution read the same publication, and two
        browsers share no ETag - so without a server-side byte cache each paid
        the whole query, the whole shaping, and the whole gzip again.
        """
        enabled, publication, facts = published()
        with enabled, publication, facts as fetch:
            first = function_app.los_facts(facts_request())
            second = function_app.los_facts(facts_request())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(decode(second)["data"], ROWS)

    def test_the_fact_query_is_told_which_publication_to_read(self):
        """Resolving the identity up front must not add a round trip for it."""
        enabled, publication, facts = published()
        with enabled, publication, facts as fetch:
            function_app.los_facts(facts_request())

        self.assertEqual(fetch.call_args.args[3], PUBLICATION)

    def test_a_new_publication_invalidates_the_previous_validator(self):
        enabled, publication, facts = published()
        with enabled, publication, facts:
            before = function_app.los_facts(facts_request())
        enabled, publication, facts = published(
            publication=LosPublication(43, PUBLISHED_AT)
        )
        with enabled, publication, facts:
            after = function_app.los_facts(facts_request())

        self.assertNotEqual(before.headers["ETag"], after.headers["ETag"])

    def test_a_new_publication_is_not_served_from_the_previous_bytes(self):
        """The validator and the cache key have to move together.

        Keyed on the range alone, a fresh publication would have been answered
        out of the previous publication's body under a new ETag - a stale answer
        the browser has just been told is current.
        """
        enabled, publication, facts = published()
        with enabled, publication, facts:
            function_app.los_facts(facts_request())

        moved = [dict(ROWS[0], bookingCount=99)]
        enabled, publication, facts = published(
            rows=moved, publication=LosPublication(43, PUBLISHED_AT)
        )
        with enabled, publication, facts:
            after = function_app.los_facts(facts_request())

        self.assertEqual(decode(after)["data"], moved)

    def test_a_different_range_is_a_different_validator(self):
        enabled, publication, facts = published()
        with enabled, publication, facts:
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
            function_app, "los_read_model_enabled", return_value=False
        ), patch.object(
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
