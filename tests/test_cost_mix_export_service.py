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
    """Answers information_schema probes only - the builders never run a query.

    Two kinds of probe now: which columns a table has, and what type one of them
    is. The type decides whether a person count is a number to read or a Mews
    PersonCounts list to sum, so a fake that only answered the first would let
    that branch go untested.
    """

    def __init__(self, columns_by_table, types_by_column=None):
        self.columns_by_table = columns_by_table
        self.types_by_column = types_by_column or {}
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters=None):
        if "data_type" in query:
            declared = self.types_by_column.get(parameters[-1])
            self._result = [{"data_type": declared}] if declared else []
            return
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
    # The relation sql/export/arr_dep_data.sql counts total_departures from. The
    # mix has to come from the same one, or it cannot be guaranteed to sum to the
    # total it apportions.
    "staging.room_nights_source": {
        "hotel_name", "reservation_id", "number", "start_utc", "end_utc",
        "night_start_utc", "canceled_utc",
    },
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

    def test_it_counts_departures_exactly_as_the_departure_total_does(self):
        query = sql_of(self.build(FULL_MIRROR))

        # Same relation, same filter, same distinct-reservation count, same
        # enterprise join as sql/export/arr_dep_data.sql - only partitioned by two
        # more dimensions. That is what makes this mix sum to total_departures by
        # construction rather than by coincidence. It was built from
        # reservation_current first, which could not promise that.
        self.assertIn('FROM "staging"."room_nights_source" nights', query)
        # The distinct-reservation count is split across the collapsing CTE and
        # the count over it: SELECT DISTINCT reduces the room nights to one row per
        # reservation per departure date - which is the grouping arr_dep_data.sql
        # counts distinct reservations within - and this counts them.
        self.assertIn('nights."reservation_id" AS reservation_key', query)
        self.assertIn("count(DISTINCT reservation_key)::int AS departures", query)
        self.assertIn("trim(enterprise.name) = departing.hotel_name", query)
        self.assertIn('trim(nights."hotel_name") AS hotel_name', query)
        self.assertIn('AND nights."canceled_utc" IS NULL', query)
        # Departure date, not arrival: cleaning is charged when the room is
        # vacated, and end_utc in Stockholm time is what arr_dep_data uses.
        self.assertIn(
            "(nights.\"end_utc\" AT TIME ZONE 'Europe/Stockholm')::date", query
        )
        self.assertNotIn("count(*)::int AS departures", query)

        # And the two dimensions the rulebook needs, from the reservation.
        self.assertIn('trim(category."space_name")::text AS category_name', query)
        self.assertIn(
            'greatest(1, (coalesce("reservation"."adult_count", 0)'
            ' + coalesce("reservation"."child_count", 0))::int)',
            query,
        )

    def test_a_room_with_no_counts_recorded_is_still_one_guest(self):
        # Costing it at zero minutes would understate a day silently. A
        # PersonCounts list that is absent, empty or malformed sums to nothing and
        # lands on one guest through the same floor.
        self.assertIn("greatest(1,", sql_of(self.build(FULL_MIRROR)))

    def _without_scalar_counts(self, **types):
        columns = dict(FULL_MIRROR)
        columns["reservation_current"] = (
            FULL_MIRROR["reservation_current"]
            - {"adult_count", "child_count"} | {"person_counts"}
        )
        cost_source_service._reset_column_cache()
        return sql_of(
            mix.build_mix_export(
                "departure_mix", FakeCursor(columns, {"person_counts": types["as_type"]})
            )
        )

    def test_a_person_counts_list_is_summed_across_its_age_categories(self):
        # Mews PersonCounts is not a number: it is one entry per age category,
        # [{"Count": 1, …}, {"Count": 3, …}], so four guests. Reading the column
        # would have been a type error; ignoring it costed every room at one guest.
        query = self._without_scalar_counts(as_type="jsonb")

        self.assertIn("jsonb_array_elements(", query)
        self.assertIn("sum(coalesce(", query)
        self.assertIn("entry ->> 'Count'", query)
        # Both casings, in case the ETL normalised the keys.
        self.assertIn("entry ->> 'count'", query)
        # Guarded: jsonb_array_elements raises on anything that is not an array,
        # and one malformed row must not fail the whole import.
        self.assertIn("jsonb_typeof(", query)
        self.assertIn("ELSE '[]'::jsonb END", query)

    def test_the_list_is_read_whether_the_mirror_landed_it_as_json_or_text(self):
        # Cast rather than assumed: json, jsonb and text holding the same document
        # all cast to jsonb the same way.
        for declared in ("jsonb", "json", "text", "character varying"):
            query = self._without_scalar_counts(as_type=declared)
            self.assertIn("jsonb_array_elements(", query, declared)
            self.assertIn('"person_counts"::jsonb', query, declared)

    def test_a_flattened_person_count_is_read_as_a_number_not_a_list(self):
        # A mirror that flattened PersonCounts into an integer but kept the plural
        # name would otherwise be read as an empty list, and every room costed at
        # one guest - wrong, and silently so. The declared type decides, not the
        # name.
        for declared in ("integer", "smallint", "bigint", "numeric"):
            query = self._without_scalar_counts(as_type=declared)
            self.assertNotIn("jsonb_array_elements", query, declared)
            self.assertIn('coalesce("reservation"."person_counts", 0)', query, declared)

    def test_adult_and_child_counts_win_over_a_person_count_column(self):
        # Adding a total that already includes children to a child count would
        # double the occupancy and cost every family room at the wrong row.
        columns = dict(FULL_MIRROR)
        columns["reservation_current"] = (
            FULL_MIRROR["reservation_current"] | {"person_counts"}
        )
        cost_source_service._reset_column_cache()
        query = sql_of(
            mix.build_mix_export(
                "departure_mix", FakeCursor(columns, {"person_counts": "jsonb"})
            )
        )

        self.assertNotIn("person_counts", query)
        self.assertIn('coalesce("reservation"."adult_count", 0)', query)
        self.assertIn('coalesce("reservation"."child_count", 0)', query)

    def test_the_guest_count_is_settled_before_the_rows_are_grouped(self):
        # Grouping by a scalar subquery would evaluate the PersonCounts sum again
        # as a grouping key, on top of once per room night if the collapsing CTE
        # were not there. Classify, then group.
        query = sql_of(self.build(FULL_MIRROR))

        self.assertIn("WITH departing AS (", query)
        self.assertIn("SELECT DISTINCT", query)
        self.assertIn(") classified", query)
        self.assertTrue(
            query.index(") classified") < query.index("GROUP BY 1, 2, 3, 4, 5, 6"),
            "the classified level must close before the grouping one",
        )

    def test_the_reservation_join_is_skipped_when_the_nights_carry_both(self):
        # Joining reservation_current across every room night is not free, so it
        # is only paid for when the category or the counts are not on the nights.
        columns = dict(FULL_MIRROR)
        columns["staging.room_nights_source"] = (
            FULL_MIRROR["staging.room_nights_source"]
            | {"requested_category_id", "adult_count", "child_count"}
        )
        query = sql_of(self.build(columns))

        self.assertNotIn("JOIN reservation_current", query)
        # Both are carried out of the collapsing CTE instead, where they are
        # per-reservation constants and so cannot split a reservation in two.
        self.assertIn('coalesce("nights"."adult_count", 0)', query)
        self.assertIn('nights."requested_category_id" AS category_key', query)
        self.assertIn("category.id::text = departing.category_key::text", query)

    def test_a_mirror_missing_any_required_column_skips_the_dataset(self):
        # Returning None is the whole safety property: an UndefinedColumn here
        # would fail the nightly import and take the five working datasets with
        # it, and the page has a documented fallback for a missing mix.
        for table, missing in (
            ("staging.room_nights_source", "end_utc"),
            ("staging.room_nights_source", "hotel_name"),
            ("staging.room_nights_source", "reservation_id"),
            ("reservation_current", "requested_category_id"),
        ):
            columns = dict(FULL_MIRROR)
            columns[table] = FULL_MIRROR[table] - {missing}
            cost_source_service._reset_column_cache()
            self.assertIsNone(self.build(columns), f"{table}.{missing}")

        # Guest counts have to be somewhere - on the nights or on the reservation.
        cost_source_service._reset_column_cache()
        columns = dict(FULL_MIRROR)
        columns["reservation_current"] = (
            FULL_MIRROR["reservation_current"] - {"adult_count", "child_count"}
        )
        self.assertIsNone(self.build(columns))

        # A missing table raises out of the resolver rather than resolving to
        # None, and has to skip the dataset just the same.
        for table in ("staging.room_nights_source", "resource_category_current"):
            cost_source_service._reset_column_cache()
            columns = dict(FULL_MIRROR)
            columns[table] = set()
            self.assertIsNone(self.build(columns), table)

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

    def test_the_mix_reads_the_rate_name_from_history(self):
        # This is the one that matters most: the mix is matched against rate
        # names an operator saved in Cost Input, so it has to see the same
        # stable name the picker offered them. Reading the current row meant a
        # renamed rate silently stopped matching its own rule, and the cost it
        # carried quietly moved to the fallback percentage.
        columns = dict(FULL_MIRROR)
        columns["rate_history"] = {"id", "name", "created_utc"}
        query = sql_of(self.build(columns))

        self.assertIn("rate_history", query)
        self.assertIn("JOIN LATERAL", query)
        self.assertIn('history."created_utc" DESC', query)
        self.assertIn("LIMIT 1", query)
        self.assertNotIn("JOIN rate_current", query)
        # Left, so a reservation whose rate has no name keeps its origin and
        # agency in the weighting instead of dropping out of the mix.
        self.assertIn("LEFT JOIN LATERAL", query)
        self.assertIn("history.id = reservation.\"rate_id\"", query)

    def test_a_mirror_without_rate_history_still_reads_the_current_row(self):
        query = sql_of(self.build(FULL_MIRROR))
        self.assertNotIn("rate_history", query)
        self.assertIn("LEFT JOIN rate_current", query)
        self.assertIn('trim(rate."rate_name")', query)

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
