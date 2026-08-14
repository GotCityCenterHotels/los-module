import json
import os
import unittest

from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "integration_db")
os.environ.setdefault("DB_USER", "readonly")
os.environ.setdefault("DB_PASSWORD", "not-used")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "app-test")
os.environ.setdefault("POSTGRES_USER", "app-test")
os.environ.setdefault("POSTGRES_PASSWORD", "not-used")

import function_app

from queries.los_facts import LOS_FACTS_SQL
from queries.los_sync import AFFECTED_RESERVATIONS_SQL
from services import los_sync_service


ROOT = Path(__file__).resolve().parent.parent


class FakeRequest:
    params = {}
    headers = {}

    def __init__(self, body=None):
        self.body = body or {}

    def get_json(self):
        return self.body


class FakeOut:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class FakeTimer:
    past_due = False


class FakeQueueMessage:
    dequeue_count = 1

    def __init__(self, job_id):
        self.job_id = job_id

    def get_json(self):
        return {"jobId": self.job_id}


class LosReadModelDefinitionTests(unittest.TestCase):
    def test_migration_is_additive_and_unpartitioned(self):
        migration = (
            ROOT / "sql" / "migrations" / "008_los_read_model.sql"
        ).read_text(encoding="utf-8").lower()
        for table in (
            "los_sync_runs",
            "reservation_los_fact",
            "los_reservation_identity",
            "reservation_los_daily",
            "los_publication",
        ):
            self.assertIn(f"functions.{table}", migration)
        self.assertNotIn("partition by", migration)
        self.assertIn("night_count = los::bigint * booking_count", migration)

    def test_raw_query_bypasses_view_and_uses_enterprise_identity(self):
        normalized = LOS_FACTS_SQL.lower()
        self.assertNotIn("staging.room_nights_source", normalized)
        self.assertIn("current_reservations as materialized", normalized)
        self.assertIn("ly_reservations as materialized", normalized)
        self.assertIn("ec.id = sc.enterprise_id", normalized)
        self.assertNotIn("trim(ec.name) =", normalized)

    def test_delta_tracks_every_identity_source(self):
        normalized = AFFECTED_RESERVATIONS_SQL.lower()
        for relation in (
            "reservation_current",
            "order_item_current",
            "service_current",
            "enterprise_current",
        ):
            self.assertIn(relation, normalized)
        self.assertIn("reservation_id", normalized)
        self.assertIn("snapshot_valid_from", normalized)

    def test_aggregate_builds_both_bases_and_all_scenarios(self):
        normalized = los_sync_service.AGGREGATE_SQL.lower()
        self.assertIn("'samedate'::text", normalized)
        self.assertIn("'sameweekday'::text", normalized)
        for scenario in ("'current'::text", "'ly'::text", "'spit'::text"):
            self.assertIn(scenario, normalized)
        self.assertIn("cancelled_date", normalized)
        self.assertIn("created_date", normalized)

    def test_integration_connections_are_forced_read_only(self):
        for path in (ROOT / "database.py", ROOT / "shared" / "db.py"):
            source = path.read_text(encoding="utf-8")
            self.assertIn("default_transaction_read_only=on", source)


class LosQueueTests(unittest.TestCase):
    def test_manual_full_sync_is_queued(self):
        output = FakeOut()
        with patch.dict(os.environ, {"LOS_SYNC_ENABLED": "true"}), patch.object(
            function_app,
            "create_import_job",
            return_value=({"jobId": "los-job", "status": "queued"}, True),
        ) as create:
            response = function_app.los_import(
                FakeRequest({"mode": "full"}), output
            )
        self.assertEqual(response.status_code, 202)
        create.assert_called_once_with("los", "full", {"mode": "full"})
        self.assertEqual(json.loads(output.value)["jobId"], "los-job")

    def test_worker_executes_los_job(self):
        job = {
            "job_type": "los",
            "operation": "delta",
            "payload": {"mode": "delta"},
        }
        with patch.object(
            function_app, "claim_import_job", return_value=job
        ), patch.object(
            function_app,
            "sync_los",
            return_value={"status": "success", "runId": 12},
        ) as sync, patch.object(
            function_app, "complete_import_job"
        ) as complete, patch.object(function_app, "log_pool_stats"):
            function_app.import_job_worker(FakeQueueMessage("los-job"))
        sync.assert_called_once_with("delta")
        self.assertEqual(complete.call_args.args[0], "los-job")

    def test_timer_is_disabled_by_default(self):
        output = FakeOut()
        with patch.dict(os.environ, {"LOS_SYNC_ENABLED": "false"}), patch.object(
            function_app, "create_import_job"
        ) as create:
            function_app.los_data_timer(FakeTimer(), output)
        create.assert_not_called()
        self.assertIsNone(output.value)

if __name__ == "__main__":
    unittest.main()
