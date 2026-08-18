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


# The room-nights view as the source actually defines it. Everything the departure
# mix needs is already resolved here - both category ids, and a person_count that
# is the summed PersonCounts list - which is why the expected plan joins nothing
# but the category name.
NIGHTS_VIEW = {
    "reservation_id", "number", "state", "hotel_name", "start_utc", "end_utc",
    "scheduled_start_utc", "actual_start_utc", "scheduled_end_utc",
    "actual_end_utc", "created_utc", "person_count",
    "requested_resource_category_id", "requested_space_name",
    "assigned_resource_id", "assigned_room_name",
    "assigned_resource_category_id", "assigned_space_name", "night_id",
    "accounting_state", "amount_net_value", "night_start_utc", "consumed_utc",
    "canceled_utc",
}

# The same view stripped to the departure columns alone, which forces the two
# extra dimensions to come from reservation_current instead. Kept as a fixture
# because that fallback is the path a mirror without the enriched view takes.
NIGHTS_LEAN = {
    "hotel_name", "reservation_id", "number", "start_utc", "end_utc",
    "night_start_utc", "canceled_utc",
}

FULL_MIRROR = {
    # The relation sql/export/arr_dep_data.sql counts total_departures from. The
    # mix has to come from the same one, or it cannot be guaranteed to sum to the
    # total it apportions.
    "staging.room_nights_source": set(NIGHTS_VIEW),
    "reservation_current": {
        "id", "number", "service_id", "start_utc", "end_utc", "canceled_utc",
        "origin", "travel_agency_id", "rate_id", "requested_category_id",
        "person_counts",
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

    # The types the source declares for the columns these tests touch.
    TYPES = {
        "person_count": "bigint", "person_counts": "jsonb",
        "id": "uuid", "reservation_id": "uuid",
    }

    def build(self, columns, types=None):
        # Through build_mix_export, which is how the pipeline reaches it: a
        # missing TABLE raises out of the resolver rather than resolving to None,
        # and that has to skip the dataset too.
        return mix.build_mix_export(
            "departure_mix", FakeCursor(columns, types or self.TYPES)
        )

    def _lean_nights(self, **reservation_types):
        """The mirror without the enriched view, so the fallback path is exercised."""
        columns = dict(FULL_MIRROR)
        columns["staging.room_nights_source"] = set(NIGHTS_LEAN)
        cost_source_service._reset_column_cache()
        return sql_of(
            self.build(columns, {**self.TYPES, **reservation_types})
        )

    def test_it_splits_one_cleaning_evenly_across_occupied_nights(self):
        query = sql_of(self.build(FULL_MIRROR))

        # The room-night relation provides both the occupied date and the stay
        # length. Every reservation contributes 1 / nights on every night.
        self.assertIn('FROM "staging"."room_nights_source" nights', query)
        self.assertIn('nights."reservation_id" AS reservation_key', query)
        self.assertIn("count(*)::numeric AS stay_nights", query)
        self.assertIn("sum(1::numeric / stay_nights) AS allocated_cleanings", query)
        self.assertIn("trim(enterprise.name) = occupied.hotel_name", query)
        self.assertIn('trim(nights."hotel_name") AS hotel_name', query)
        self.assertIn('AND nights."canceled_utc" IS NULL', query)
        self.assertIn(
            "(nights.\"night_start_utc\" AT TIME ZONE 'Europe/Stockholm')::date",
            query,
        )
        self.assertNotIn("AS departures", query)

        # The category NAME still comes from resource_category_current, not from
        # the view's own requested_space_name. The view reads it out of
        # resource_category_history, which can hold a superseded spelling, and the
        # cleaning rows the page matches against were saved with the name
        # list_cleaning_categories showed - which is the current one.
        self.assertIn('trim(category."space_name")::text AS category_name', query)

    def test_the_enriched_view_needs_no_join_to_the_reservation_at_all(self):
        # The view already exposes both category ids and a person_count that is the
        # summed PersonCounts list, so joining reservation_current across every
        # departing reservation would only recompute what is already there.
        query = sql_of(self.build(FULL_MIRROR))

        self.assertNotIn("JOIN reservation_current", query)
        self.assertNotIn("jsonb_array_elements", query)
        self.assertIn('greatest(1, (coalesce("nights"."person_count", 0))::int)', query)

    def test_the_room_actually_occupied_is_the_one_costed(self):
        # An upgrade from a double to a suite is cleaned as a suite. Assigned is the
        # room that was occupied; requested is what was booked, and is the fallback
        # for a stay with no room assigned - assigned_resource_category_id comes
        # from a LEFT JOIN in the view and is null in that case.
        query = sql_of(self.build(FULL_MIRROR))

        self.assertIn(
            'coalesce("nights"."assigned_resource_category_id",'
            ' "nights"."requested_resource_category_id") AS category_key',
            query,
        )

    def test_either_name_for_the_room_nights_relation_resolves(self):
        # arr_dep_data.sql reads staging.room_nights_source; the view behind it is
        # staging.room_nights_current. A deployment may have either or both, and
        # source is preferred because that is what the total is counted from.
        only_current = {
            key: value for key, value in FULL_MIRROR.items()
            if key != "staging.room_nights_source"
        }
        only_current["staging.room_nights_current"] = set(NIGHTS_VIEW)
        cost_source_service._reset_column_cache()
        query = sql_of(self.build(only_current))

        self.assertIn('FROM "staging"."room_nights_current" nights', query)

    def test_a_room_with_no_counts_recorded_is_still_one_guest(self):
        # Costing it at zero minutes would understate a day silently. A
        # PersonCounts list that is absent, empty or malformed sums to nothing and
        # lands on one guest through the same floor.
        self.assertIn("greatest(1,", sql_of(self.build(FULL_MIRROR)))

    def test_a_person_counts_list_is_summed_across_its_age_categories(self):
        # Mews PersonCounts is not a number: it is one entry per age category,
        # [{"Count": 1, …}, {"Count": 3, …}], so four guests. Reading the column as
        # a number would have been a type error; not matching it at all - which is
        # what a candidate list of `person_count` singular did - skipped the whole
        # dataset and left cleaning on its flat average.
        query = self._lean_nights()

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
            query = self._lean_nights(person_counts=declared)
            self.assertIn("jsonb_array_elements(", query, declared)
            self.assertIn('"person_counts"::jsonb', query, declared)

    def test_a_flattened_person_count_is_read_as_a_number_not_a_list(self):
        # A mirror that flattened PersonCounts into an integer but kept the plural
        # name would otherwise be read as an empty list, and every room costed at
        # one guest - wrong, and silently so. The declared type decides, not the
        # name.
        for declared in ("integer", "smallint", "bigint", "numeric"):
            query = self._lean_nights(person_counts=declared)
            self.assertNotIn("jsonb_array_elements", query, declared)
            self.assertIn('coalesce("reservation"."person_counts", 0)', query, declared)

    def test_adult_and_child_counts_win_over_a_person_count_column(self):
        # Adding a total that already includes children to a child count would
        # double the occupancy and cost every family room at the wrong row.
        columns = dict(FULL_MIRROR)
        columns["staging.room_nights_source"] = set(NIGHTS_LEAN)
        columns["reservation_current"] = (
            FULL_MIRROR["reservation_current"] | {"adult_count", "child_count"}
        )
        cost_source_service._reset_column_cache()
        query = sql_of(self.build(columns))

        self.assertNotIn("person_counts", query)
        self.assertIn('coalesce("reservation"."adult_count", 0)', query)
        self.assertIn('coalesce("reservation"."child_count", 0)', query)

    def test_matching_key_types_join_without_a_cast(self):
        # The cast is correct either way but throws away the index on
        # reservation_current's primary key, so it is kept only for the mirrors
        # that type the two keys differently - where uuid = text is `operator does
        # not exist`, a 500 rather than a wrong answer.
        matching = self._lean_nights()
        self.assertIn(
            "JOIN reservation_current reservation "
            "ON reservation.id = occupied.reservation_key",
            matching,
        )

        differing = self._lean_nights(reservation_id="text")
        self.assertIn(
            "ON reservation.id::text = occupied.reservation_key::text", differing
        )

    def test_the_guest_count_is_settled_before_the_rows_are_grouped(self):
        # Grouping by a scalar subquery would evaluate the PersonCounts sum again
        # as a grouping key, on top of once per room night if the collapsing CTE
        # were not there. Classify, then group.
        query = sql_of(self.build(FULL_MIRROR))

        self.assertIn("WITH occupied_nights AS (", query)
        self.assertIn("stay_lengths AS (", query)
        self.assertIn("SELECT DISTINCT", query)
        self.assertIn(") classified", query)
        self.assertTrue(
            query.index(") classified") < query.index("GROUP BY 1, 2, 3, 4, 5, 6"),
            "the classified level must close before the grouping one",
        )

    def test_the_departure_columns_are_required_on_the_nights_relation(self):
        # Returning None is the whole safety property: an UndefinedColumn here
        # would fail the nightly import and take the five working datasets with
        # it, and the page has a documented fallback for a missing mix.
        for missing in ("night_start_utc", "hotel_name", "reservation_id"):
            columns = dict(FULL_MIRROR)
            columns["staging.room_nights_source"] = NIGHTS_VIEW - {missing}
            cost_source_service._reset_column_cache()
            self.assertIsNone(self.build(columns), missing)

    def test_a_dimension_missing_from_both_relations_skips_the_dataset(self):
        # Each of the two extra dimensions is looked for on the nights and then on
        # the reservation, so a case only counts as missing when it is absent from
        # both. Removing it from one just moves where it is read from.
        every_category = set(mix.ASSIGNED_CATEGORY_COLUMNS) | set(
            mix.REQUESTED_CATEGORY_COLUMNS
        )
        every_count = set(mix.RESERVATION_PERSON_COLUMNS) | set(
            mix.RESERVATION_ADULT_COLUMNS
        ) | set(mix.RESERVATION_CHILD_COLUMNS)

        for dropped in (every_category, every_count):
            columns = dict(FULL_MIRROR)
            columns["staging.room_nights_source"] = NIGHTS_VIEW - dropped
            columns["reservation_current"] = (
                FULL_MIRROR["reservation_current"] - dropped
            )
            cost_source_service._reset_column_cache()
            self.assertIsNone(self.build(columns), sorted(dropped)[:2])

    def test_a_missing_table_skips_the_dataset_rather_than_raising(self):
        # A missing table raises out of the resolver instead of resolving to None,
        # and has to reach the same outcome.
        for table in ("resource_category_current",):
            cost_source_service._reset_column_cache()
            columns = dict(FULL_MIRROR)
            columns[table] = set()
            self.assertIsNone(self.build(columns), table)

        # Neither name for the room nights: this is the relation the departure
        # total itself is counted from, so there is nothing to apportion.
        cost_source_service._reset_column_cache()
        columns = {
            key: value for key, value in FULL_MIRROR.items()
            if key != "staging.room_nights_source"
        }
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
