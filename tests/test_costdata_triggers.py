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

import function_app
from cost_database import cost_pool
from database import pool
from shared import pipeline


cost_pool.close()
pool.close()


_registered_functions = None


def registered_functions():
    """function_app.app.get_functions(), computed once for the whole module.

    It is not idempotent: the second call in a process re-registers every name and
    then rejects the list for containing duplicates, so two tests each asking for
    it means whichever runs second fails with a confusing complaint about a
    function neither of them mentions.
    """
    global _registered_functions
    if _registered_functions is None:
        _registered_functions = function_app.app.get_functions()
    return _registered_functions


def http_routes():
    """{route: authLevel} for every HTTP-triggered function."""
    levels = {}
    for registered in registered_functions():
        for binding in registered.get_bindings():
            shape = binding.get_dict_repr()
            if shape.get("type") == "httpTrigger":
                levels[shape.get("route")] = (
                    str(shape.get("authLevel")).lower().replace("authlevel.", "")
                )
    return levels


class FakeTimer:
    past_due = False


class FakeOut:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class FakeQueueMessage:
    dequeue_count = 1

    def __init__(self, job_id):
        self.job_id = job_id

    def get_json(self):
        return {"jobId": self.job_id}


class FakeRequest:
    def __init__(self, body, params=None):
        self.body = body
        self.params = params or {}

    def get_json(self):
        return self.body


class CostDataTriggerTests(unittest.TestCase):
    def test_property_sync_creates_database_a_target_before_transfer(self):
        expected = {"export_rows": 8, "import_rows": 8}

        with patch(
            "services.cost_schema_service.ensure_cost_settings_schema",
        ) as ensure_schema, patch.object(
            pipeline,
            "transfer_dataset",
            return_value=expected,
        ) as transfer, patch(
            "cost_database.cost_pool.connection",
        ) as pool_connection, patch.object(
            pipeline,
            "advance_cost_publication",
        ) as publish:
            cursor = pool_connection.return_value.__enter__.return_value.cursor
            cursor = cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = (8,)
            result = pipeline.run_dataset("properties")

        ensure_schema.assert_called_once_with()
        transfer.assert_called_once_with(
            export_sql_file="export/cost_properties.sql",
            import_sql_file="import/upsert_cost_properties.sql",
            batch_size=5000,
        )
        self.assertEqual(result, {
            "dataset": "properties",
            **expected,
            "verified_rows": 8,
        })
        publish.assert_called_once_with("import:properties")

    def test_property_sync_rejects_an_empty_source_result(self):
        with patch(
            "services.cost_schema_service.ensure_cost_settings_schema",
        ), patch.object(
            pipeline,
            "transfer_dataset",
            return_value={"export_rows": 0, "import_rows": 0},
        ), self.assertRaisesRegex(RuntimeError, "returned no GCCH properties"):
            pipeline.run_dataset("properties")

    def test_spit_uses_the_read_model_runner_and_advances_publication(self):
        expected = {
            "status": "success",
            "export_rows": 100,
            "import_rows": 20,
            "pruned_rows": 0,
        }
        with patch(
            "services.cost_schema_service.ensure_cost_settings_schema",
        ), patch(
            "services.cost_spit_sync_service.sync_cost_spit",
            return_value=expected,
        ) as sync_spit, patch.object(
            pipeline, "advance_cost_publication"
        ) as publish:
            result = pipeline.run_dataset("spit")

        sync_spit.assert_called_once_with()
        publish.assert_called_once_with("import:spit")
        self.assertEqual(result, {"dataset": "spit", **expected})

    def test_timer_runs_every_dataset(self):
        output = FakeOut()
        with patch.object(
            function_app,
            "create_import_job",
            return_value=({"jobId": "job-1"}, True),
        ) as create:
            function_app.cost_data_timer(FakeTimer(), output)

        create.assert_called_once_with("cost", "all", {"dataset": "all"})
        self.assertIn("job-1", output.value)

    def test_manual_trigger_runs_a_selected_dataset(self):
        output = FakeOut()
        with patch.object(
            function_app,
            "create_import_job",
            return_value=({"jobId": "job-2", "status": "queued"}, True),
        ) as create:
            response = function_app.cost_data_import(
                FakeRequest({"dataset": "parking"}),
                output,
            )

        create.assert_called_once_with(
            "cost", "parking", {"dataset": "parking"}
        )
        self.assertEqual(response.status_code, 202)
        self.assertIn("job-2", output.value)
        self.assertEqual(response.headers["Retry-After"], "2")

    def test_manual_trigger_defaults_to_all_datasets(self):
        output = FakeOut()
        with patch.object(
            function_app,
            "create_import_job",
            return_value=({"jobId": "job-3", "status": "queued"}, True),
        ) as create:
            response = function_app.cost_data_import(FakeRequest({}), output)

        create.assert_called_once_with("cost", "all", {"dataset": "all"})
        self.assertEqual(response.status_code, 202)

    def test_manual_trigger_rejects_unknown_dataset_before_queueing(self):
        with patch.object(function_app, "create_import_job") as create:
            response = function_app.cost_data_import(
                FakeRequest({"dataset": "unknown"}), FakeOut()
            )
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    def test_worker_executes_cost_job_and_records_completion(self):
        job = {
            "job_type": "cost",
            "operation": "parking",
            "payload": {"dataset": "parking"},
        }
        expected = {"dataset": "parking", "export_rows": 2, "import_rows": 2}
        with patch.object(
            function_app, "claim_import_job", return_value=job
        ), patch.object(
            function_app, "run_dataset", return_value=expected
        ) as run, patch.object(
            function_app, "complete_import_job"
        ) as complete, patch.object(function_app, "log_pool_stats"):
            function_app.import_job_worker(FakeQueueMessage("job-4"))
        run.assert_called_once_with("parking")
        self.assertEqual(complete.call_args.args[0], "job-4")
        self.assertEqual(complete.call_args.args[1]["status"], "success")

    def test_worker_marks_transient_failure_for_retry(self):
        job = {
            "job_type": "cost",
            "operation": "parking",
            "payload": {"dataset": "parking"},
        }
        with patch.object(
            function_app, "claim_import_job", return_value=job
        ), patch.object(
            function_app, "run_dataset", side_effect=RuntimeError("source unavailable")
        ), patch.object(
            function_app, "fail_import_job"
        ) as fail, patch.object(function_app, "log_pool_stats"), self.assertRaises(
            RuntimeError
        ):
            function_app.import_job_worker(FakeQueueMessage("job-5"))
        fail.assert_called_once()
        self.assertTrue(fail.call_args.args[2])

    def test_v2_function_app_registers_manual_and_timer_triggers(self):
        function_names = {
            registered.get_function_name()
            for registered in registered_functions()
        }
        routes = set(http_routes())

        self.assertIn("CostDataImport", function_names)
        self.assertIn("CostDataTimer", function_names)
        self.assertIn("SupplementDataImport", function_names)
        self.assertIn("SupplementDataTimer", function_names)
        self.assertIn("LosDataImport", function_names)
        self.assertIn("LosDataTimer", function_names)
        self.assertIn("ImportJobWorker", function_names)
        self.assertIn("ImportJobStatus", function_names)
        self.assertIn("costdata/properties", routes)
        self.assertIn("costdata/settings/{enterprise_id}", routes)
        self.assertIn("imports/{job_id}", routes)
        self.assertIn("los/import", routes)
        self.assertIn("los/status", routes)
        self.assertNotIn("costdata/settings/hotels", routes)

    def test_every_costdata_route_is_reachable_from_the_page(self):
        """The import is guarded by the platform, not by a key the operator lacks.

        It was FUNCTION on the premise that the Function App answers on its own
        public hostname. It does not: the app is a Static Web Apps linked backend,
        so App Service Authentication refuses a direct request before the route is
        reached, and the site is behind Static Web Apps password protection on top.
        The key gated nothing except the operator, who could not use the button at
        all - while costdata/settings, a PUT that rewrites every cost figure for
        every hotel, has always been ANONYMOUS behind those same two layers.
        """
        levels = http_routes()
        cost_routes = {
            route: level for route, level in levels.items()
            if route and route.startswith("costdata/")
        }
        self.assertIn("costdata/import", cost_routes)
        # One posture for the whole family, so no single route is the odd one out
        # that quietly cannot be called.
        self.assertEqual(set(cost_routes.values()), {"anonymous"}, cost_routes)
        # Polling a job needs no credential either, or the button could start an
        # import and then not be able to watch it.
        self.assertEqual(levels.get("imports/{job_id}"), "anonymous")
        # The other two import families stay shut: nothing in the application calls
        # them, so nothing is blocked by that.
        for route in ("los/import", "supplement/import"):
            self.assertEqual(levels.get(route), "function", route)


if __name__ == "__main__":
    unittest.main()
