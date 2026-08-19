"""What a cold HTTP start is allowed to import.

Azure Functions imports function_app to index its triggers, so anything reachable
from module scope is paid for on every cold start - including by a request that
can never touch it. The import pipeline and the two sync services are reachable
only from the queue worker and the enqueue routes, and they were costing about
37ms of module graph measured on a developer machine, more on a 1-vCPU Flex
instance.

The deferral is easy to undo by accident: adding a top-level `from
shared.pipeline import ...` back to function_app would silently restore the cost
and nothing else would notice. This runs the import in a fresh interpreter and
checks.
"""

import os
import subprocess
import sys
import unittest

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# Placeholder credentials: the pools are constructed at import, so function_app
# cannot be imported at all without them. Nothing here connects.
CHILD_ENV = {
    "DB_HOST": "localhost",
    "DB_NAME": "integration_db",
    "DB_USER": "readonly",
    "DB_PASSWORD": "not-used",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_DB": "app-test",
    "POSTGRES_USER": "app-test",
    "POSTGRES_PASSWORD": "not-used",
}

# Reachable only from import_job_worker, cost_data_import, los_import and
# supplement_import. A read path cannot get to any of them.
WORKER_ONLY_MODULES = (
    "shared.pipeline",
    "shared.sql_runner",
    "services.los_sync_service",
    "services.supplement_sync_service",
    "services.cost_mix_export_service",
    "queries.los_sync",
)

PROBE = """
import json, sys
import function_app
print(json.dumps({
    "loaded": sorted(m for m in sys.modules if m.startswith(("services", "queries", "shared"))),
    "attrs": {
        name: hasattr(function_app, name)
        for name in (
            "run_dataset", "run_all_datasets", "dataset_names",
            "sync_los", "sync_supplement",
        )
    },
}))
"""


def import_function_app_in_a_fresh_interpreter():
    environment = dict(os.environ)
    environment.update(CHILD_ENV)
    environment["PYTHONPATH"] = str(ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=str(ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"importing function_app failed:\n{completed.stderr}"
        )
    import json

    # The pools can print a finalization warning on interpreter shutdown; the
    # payload is the last line of stdout.
    return json.loads(completed.stdout.strip().splitlines()[-1])


class ColdStartImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = import_function_app_in_a_fresh_interpreter()

    def test_no_worker_only_module_is_imported_at_module_scope(self):
        loaded = set(self.probe["loaded"])
        for module in WORKER_ONLY_MODULES:
            with self.subTest(module=module):
                self.assertNotIn(
                    module,
                    loaded,
                    f"{module} is imported on every cold start; import it "
                    f"inside the function that needs it instead",
                )

    def test_the_read_path_services_are_still_imported_eagerly(self):
        # The other half of the contract. Deferring these would move the cost onto
        # the first request of every worker rather than removing it, and would
        # break the patch sites the route tests rely on.
        loaded = set(self.probe["loaded"])
        for module in (
            "services.los_facts_service",
            "services.cost_data_service",
            "services.hotels_service",
        ):
            with self.subTest(module=module):
                self.assertIn(module, loaded)

    def test_the_deferred_names_remain_patchable_on_the_module(self):
        # Tests patch function_app.run_dataset. A bare inline import inside the
        # worker would leave nothing to patch.
        for name, present in self.probe["attrs"].items():
            with self.subTest(name=name):
                self.assertTrue(present, f"function_app.{name} is missing")


if __name__ == "__main__":
    unittest.main()
