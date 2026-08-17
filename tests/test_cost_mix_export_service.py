import os
import unittest


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "los-test")
os.environ.setdefault("DB_USER", "los-test")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

from services import cost_mix_export_service as mix
from services import cost_source_service


cost_source_service.export_pool.close()


class FakeCursor:
    """Answers information_schema probes only - the builders never run a query."""

    def __init__(self, columns_by_table):
        self.columns_by_table = columns_by_table
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters=None):
        table = parameters[0]
        if len(parameters) > 1:
            table = f"{parameters[1]}.{table}"
        self._result = [
            {"column_name": name}
            for name in self.columns_by_table.get(table, set())
        ]

    def fetchall(self):
        return self._result


FULL_MIRROR = {
    "reservation_current": {
        "id", "number", "service_id", "start_utc", "end_utc", "canceled_utc",
        "origin", "travel_agency_id", "rate_id", "requested_category_id",
        "adult_count", "child_count",
    },
    "service_current": {"id", "name", "enterprise_id"},
    "resource_category_current": {
        "id", "enterprise_id", "space_name", "capacity", "extra_capacity",
    },
    "order_item_current": {
        "id", "service_id", "service_order_id", "type", "start_utc",
        "canceled_utc", "amount_net_value",
    },
    "rate_current": {"id", "service_id", "rate_name", "is_active"},
    "staging.travel_agency": {"id", "name"},
}


def sql_of(plan, key="export_sql"):
    return " ".join(plan[key].as_string(None).split())


class DepartureMixExportTests(unittest.TestCase):
    def setUp(self):
        cost_source_service._reset_column_cache()

    def build(self, columns):
        # Through build_mix_export, which is how the pipeline reaches it: a
        # missing TABLE raises out of the resolver rather than resolving to None,
        # and that has to skip the dataset too.
        return mix.build_mix_export("departure_mix", FakeCursor(columns))

    def test_it_splits_departures_by_category_and_guest_count(self):
        query = sql_of(self.build(FULL_MIRROR))

        # Departure date, not arrival: cleaning is charged when the room is
        # vacated, which is what functions.arr_dep_data counts too.
        self.assertIn(
            "(reservation.\"end_utc\" AT TIME ZONE 'Europe/Stockholm')::date", query
        )
        self.assertIn('trim(category."space_name")::text AS category_name', query)
        self.assertIn(
            'greatest(1, (coalesce(reservation."adult_count", 0)'
            ' + coalesce(reservation."child_count", 0))::int)',
            query,
        )
        # Parity with sql/export/arr_dep_data.sql, which filters on nothing else.
        self.assertIn('AND reservation."canceled_utc" IS NULL', query)
        self.assertIn("count(*)::int AS departures", query)

    def test_a_room_with_no_counts_recorded_is_still_one_guest(self):
        # Costing it at zero minutes would understate a day silently.
        self.assertIn("greatest(1,", sql_of(self.build(FULL_MIRROR)))

    def test_a_single_person_column_is_used_only_when_there_is_no_adult_count(self):
        columns = dict(FULL_MIRROR)
        columns["reservation_current"] = (
            FULL_MIRROR["reservation_current"] - {"adult_count"} | {"person_count"}
        )
        query = sql_of(self.build(columns))

        # Adding a person count that already includes children to a child count
        # would double the occupancy and cost every family room at the wrong row.
        self.assertIn('greatest(1, coalesce(reservation."person_count", 0)::int)', query)
        self.assertNotIn('"child_count"', query)

    def test_a_mirror_missing_any_required_column_skips_the_dataset(self):
        # Returning None is the whole safety property: an UndefinedColumn here
        # would fail the nightly import and take the five working datasets with
        # it, and the page has a documented fallback for a missing mix.
        for missing in ("end_utc", "requested_category_id", "adult_count"):
            columns = dict(FULL_MIRROR)
            columns["reservation_current"] = (
                FULL_MIRROR["reservation_current"] - {missing}
            )
            if missing == "adult_count":
                columns["reservation_current"] -= {"child_count"}
            cost_source_service._reset_column_cache()
            self.assertIsNone(self.build(columns), missing)

        cost_source_service._reset_column_cache()
        columns = dict(FULL_MIRROR)
        columns["resource_category_current"] = set()
        self.assertIsNone(self.build(columns))

    def test_it_prunes_rows_this_run_did_not_re_import(self):
        prune = sql_of(self.build(FULL_MIRROR), "prune_sql")

        # A (category, occupancy) that stops having departures on a day has no
        # row for the upsert to overwrite, so it would keep its old count for good.
        self.assertIn('DELETE FROM functions."departure_mix_data"', prune)
        self.assertIn("last_seen_at < %(started_at)s", prune)
        # Bounded to the exported window, so history outside it is not deleted
        # every night.
        self.assertIn("stay_date >=", prune)


class DistributionMixExportTests(unittest.TestCase):
    def setUp(self):
        cost_source_service._reset_column_cache()

    def build(self, columns):
        return mix.build_mix_export("distribution_mix", FakeCursor(columns))

    def test_it_splits_room_revenue_by_origin_agency_and_rate(self):
        query = sql_of(self.build(FULL_MIRROR))

        self.assertIn('nullif(trim(reservation."origin"), \'\') AS origin', query)
        self.assertIn('nullif(trim(agency."name"), \'\')', query)
        self.assertIn('nullif(trim(rate."rate_name"), \'\')', query)
        self.assertIn("sum(item.amount_net_value) AS room_revenue_net", query)
        # order_item_current is the accounting-item table - total_payment_data
        # reads the same rows as payments - so without the type filter the weight
        # would include deposits and settlements, which are not revenue.
        self.assertIn("AND item.\"type\" = 'SpaceOrder'", query)
        # Both id joins cast to text: the mirror types these keys differently per
        # deployment, and a mismatch is an error, not a wrong answer.
        self.assertIn('agency."id"::text = reservation."travel_agency_id"::text', query)
        self.assertIn('rate.id::text = reservation."rate_id"::text', query)

    def test_a_mirror_with_no_agency_or_rate_still_carries_the_origin_level(self):
        columns = dict(FULL_MIRROR)
        columns["reservation_current"] = (
            FULL_MIRROR["reservation_current"] - {"travel_agency_id", "rate_id"}
        )
        columns["staging.travel_agency"] = set()
        query = sql_of(self.build(columns))

        # Origin is the level that decides most of the rulebook, so losing the two
        # below it must not lose the whole dataset.
        self.assertIn("NULL::text AS travel_agency", query)
        self.assertIn("NULL::text AS rate_name", query)
        self.assertIn('reservation."origin"', query)
        self.assertNotIn("JOIN rate_current", query)

    def test_without_an_origin_column_there_is_nothing_to_apportion_by(self):
        columns = dict(FULL_MIRROR)
        columns["reservation_current"] = (
            FULL_MIRROR["reservation_current"]
            - set(cost_source_service.RESERVATION_ORIGIN_COLUMNS)
        )
        self.assertIsNone(self.build(columns))

    def test_an_unknown_builder_name_is_rejected_rather_than_ignored(self):
        with self.assertRaises(ValueError):
            mix.build_mix_export("nope", FakeCursor(FULL_MIRROR))


class PipelineRegistrationTests(unittest.TestCase):
    def test_both_mixes_are_registered_with_a_builder_and_an_upsert(self):
        from pathlib import Path

        from shared.pipeline import DATASETS

        root = Path(__file__).resolve().parent.parent
        for name in ("departure_mix", "distribution_mix"):
            self.assertIn(name, DATASETS)
            config = DATASETS[name]
            # Built at run time, never read from a file: the source column names
            # are not knowable from this repository.
            self.assertIn(config["export_builder"], mix.BUILDERS)
            self.assertNotIn("export_sql", config)
            self.assertTrue((root / "sql" / config["import_sql"]).exists())

        # Properties has to stay first - every fact table references
        # functions.hotels - and the two expensive mixes last.
        self.assertEqual(list(DATASETS)[0], "properties")
        self.assertEqual(list(DATASETS)[-2:], ["departure_mix", "distribution_mix"])


if __name__ == "__main__":
    unittest.main()
