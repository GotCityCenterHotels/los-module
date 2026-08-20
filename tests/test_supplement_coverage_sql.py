"""Execute the Supplement coverage statement against a real PostgreSQL.

Everything else that touches this statement checks its *text*. That is not the
same as running it, which is the gap docs/LOAD_TIME_PLAN.md and the architecture
review both name: nothing in this repository executes the SQL under queries/ or
the runtime SQL inside services/ against data, so a statement can be
structurally reviewed, unit-tested by substring, and still be wrong the first
time a real planner sees it. `_refresh_coverage` is worth the exception because
it decides which stay dates fetch_supplement_grid is willing to answer for - if
it writes the wrong window, the page silently shows nothing.

Skipped unless a database is offered, the same way tests/build-frontend.test.js
skips without esbuild. CI already stands up postgres:16-alpine and exports
COST_DB_*, so it runs there; locally, point it at a throwaway container:

    docker run -d --name pg -e POSTGRES_PASSWORD=ci-postgres \\
        -e POSTGRES_DB=costdb -p 55432:5432 postgres:16-alpine
    COST_DB_HOST=127.0.0.1 COST_DB_PORT=55432 COST_DB_NAME=costdb \\
        COST_DB_USER=postgres COST_DB_PASSWORD=ci-postgres \\
        python -m unittest tests.test_supplement_coverage_sql
"""

import os
import unittest
import uuid

from datetime import date


def _dsn():
    """A DSN only when one was explicitly offered.

    The placeholders the other test modules set with setdefault would otherwise
    make this look configured and then fail to connect, so this reads the
    environment directly and requires a host that is not the placeholder.
    """
    host = os.environ.get("COST_DB_HOST")
    if not host or host == "localhost" and not os.environ.get("COST_DB_PORT"):
        return None
    if not os.environ.get("COST_DB_PASSWORD"):
        return None
    import psycopg

    return psycopg.conninfo.make_conninfo(
        host=host,
        port=os.environ.get("COST_DB_PORT", "5432"),
        dbname=os.environ.get("COST_DB_NAME", "costdb"),
        user=os.environ.get("COST_DB_USER", "postgres"),
        password=os.environ["COST_DB_PASSWORD"],
        connect_timeout=5,
    )


DSN = _dsn()
REASON = "no PostgreSQL offered via COST_DB_* (see this module's docstring)"

# Imported after the DSN decision so a bare checkout can still collect the
# module: cost_database builds a pool at import and needs these present.
os.environ.setdefault("COST_DB_NAME", "placeholder")
os.environ.setdefault("COST_DB_HOST", "localhost")
os.environ.setdefault("COST_DB_USER", "placeholder")
os.environ.setdefault("COST_DB_PASSWORD", "placeholder")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "placeholder")
os.environ.setdefault("DB_USER", "placeholder")
os.environ.setdefault("DB_PASSWORD", "placeholder")

from services import supplement_sync_service as sync_service


@unittest.skipIf(DSN is None, REASON)
class SupplementCoverageSqlTests(unittest.TestCase):
    """The statement runs, and writes the window the grid needs."""

    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.psycopg = psycopg
        try:
            cls.connection = psycopg.connect(DSN, autocommit=False)
        except Exception as error:  # pragma: no cover - environment problem
            raise unittest.SkipTest(f"cannot reach PostgreSQL: {error}")

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def setUp(self):
        # Every test runs inside a transaction that is rolled back, so the
        # schema this points at is left exactly as it was found.
        self.connection.rollback()
        self.cursor = self.connection.cursor(
            row_factory=self.psycopg.rows.dict_row
        )
        self.cursor.execute("SELECT to_regclass('functions.supplement_coverage')")
        if self.cursor.fetchone()["to_regclass"] is None:
            self.skipTest(
                "run scripts/apply_migrations.py against this database first"
            )
        self.cursor.execute("""
            INSERT INTO functions.supplement_sync_runs (mode, status)
            VALUES ('backfill', 'running') RETURNING run_id
        """)
        self.run_id = self.cursor.fetchone()["run_id"]

    def tearDown(self):
        self.connection.rollback()

    def _inventory(self, table, stay_date, snapshot_date):
        # supplement_snapshot_* is PARTITIONED BY RANGE (snapshot_date), so a
        # row whose month has no partition is rejected outright. Only running
        # this against a real database surfaces that - the production path calls
        # the same helper from _ensure_partitions.
        self.cursor.execute(
            "SELECT functions.ensure_supplement_month_partitions(%s)",
            (snapshot_date.replace(day=1),),
        )
        self.cursor.execute(
            f"""
            INSERT INTO functions.{table} (
                stay_date, hotel_code, space_room_category_id, space_room_name,
                snapshot_date, total_space, space_to_sell, inventory_quality,
                run_id
            ) VALUES (%s, 'hotel-a', %s, 'Double', %s, 10, 8, 'exact', %s)
            """,
            (stay_date, uuid.uuid4(), snapshot_date, self.run_id),
        )

    def _coverage(self):
        self.cursor.execute(
            "SELECT * FROM functions.supplement_coverage WHERE singleton"
        )
        return self.cursor.fetchone()

    def test_the_statement_executes(self):
        """The point of this module: it runs at all."""
        self._inventory("supplement_latest_inventory", date(2026, 8, 1), date(2026, 8, 8))
        self._inventory("supplement_snapshot_inventory", date(2026, 8, 20), date(2026, 8, 20))

        sync_service._refresh_coverage(self.cursor)

        self.assertIsNotNone(self._coverage())

    def test_a_stay_date_pruned_from_snapshots_is_still_covered(self):
        """The reported bug, reproduced and then fixed.

        1 August is in the permanent latest table but has been pruned from the
        snapshot table (retention drops snapshot_date > stay_date + 7). Coverage
        used to be derived from the snapshot table, so it started on 20 August
        and the grid refused 1 August despite holding its facts.
        """
        self._inventory("supplement_latest_inventory", date(2026, 8, 1), date(2026, 8, 8))
        self._inventory("supplement_latest_inventory", date(2026, 12, 31), date(2026, 8, 20))
        # Snapshots only exist for the recent window.
        self._inventory("supplement_snapshot_inventory", date(2026, 8, 20), date(2026, 8, 20))

        sync_service._refresh_coverage(self.cursor)
        coverage = self._coverage()

        self.assertEqual(coverage["minimum_stay_date"], date(2026, 8, 1))
        self.assertEqual(coverage["maximum_stay_date"], date(2026, 12, 31))
        # The snapshot window is a different question and stays honest: it is
        # what bounds the pickup curves and the SPIT column.
        self.assertEqual(coverage["minimum_snapshot_date"], date(2026, 8, 20))
        self.assertEqual(coverage["maximum_snapshot_date"], date(2026, 8, 20))

    def test_and_the_grid_would_now_serve_that_date(self):
        """Ties the SQL to the decision it drives, end to end."""
        from services import supplement_service

        self._inventory("supplement_latest_inventory", date(2026, 8, 1), date(2026, 8, 8))
        self._inventory("supplement_latest_inventory", date(2026, 12, 31), date(2026, 8, 20))
        self._inventory("supplement_snapshot_inventory", date(2026, 8, 20), date(2026, 8, 20))
        sync_service._refresh_coverage(self.cursor)
        coverage = self._coverage()

        served = supplement_service.clip_to_coverage(
            date(2026, 8, 1),
            date(2026, 8, 31),
            {
                "minimumStayDate": coverage["minimum_stay_date"].isoformat(),
                "maximumStayDate": coverage["maximum_stay_date"].isoformat(),
            },
        )

        # Nothing clipped, because 1 August is genuinely covered.
        self.assertEqual(served[0], date(2026, 8, 1))
        self.assertEqual(served[1], date(2026, 8, 31))
        self.assertIsNone(served[2])

    def test_an_empty_latest_table_skips_instead_of_failing(self):
        """All four date columns are NOT NULL; min() over nothing is NULL.

        Without the guard this aborted the whole publication transaction over
        bookkeeping. Only executing it proves the guard is placed correctly -
        WHERE after a CROSS JOIN and before ON CONFLICT is exactly the sort of
        thing a text assertion cannot check.
        """
        self._inventory("supplement_snapshot_inventory", date(2026, 8, 20), date(2026, 8, 20))
        # Nothing in supplement_latest_inventory at all.

        sync_service._refresh_coverage(self.cursor)

        self.assertIsNone(self._coverage())

    def test_an_empty_snapshot_table_also_skips(self):
        self._inventory("supplement_latest_inventory", date(2026, 8, 1), date(2026, 8, 8))

        sync_service._refresh_coverage(self.cursor)

        self.assertIsNone(self._coverage())

    def test_a_second_run_updates_the_existing_row(self):
        """ON CONFLICT, exercised rather than read."""
        self._inventory("supplement_latest_inventory", date(2026, 8, 1), date(2026, 8, 8))
        self._inventory("supplement_snapshot_inventory", date(2026, 8, 20), date(2026, 8, 20))
        sync_service._refresh_coverage(self.cursor)
        first = self._coverage()

        self._inventory("supplement_latest_inventory", date(2027, 6, 1), date(2026, 8, 20))
        sync_service._refresh_coverage(self.cursor)
        second = self._coverage()

        self.assertEqual(first["minimum_stay_date"], date(2026, 8, 1))
        self.assertEqual(second["maximum_stay_date"], date(2027, 6, 1))
        self.cursor.execute("SELECT count(*) AS n FROM functions.supplement_coverage")
        self.assertEqual(self.cursor.fetchone()["n"], 1)

    def test_retention_runs_its_prune_and_the_coverage_write_together(self):
        """_apply_retention's DELETEs and the coverage refresh, both executed."""
        # Pruned: a snapshot taken more than 7 days after the stay date.
        self._inventory("supplement_snapshot_inventory", date(2026, 8, 1), date(2026, 8, 20))
        # Kept.
        self._inventory("supplement_snapshot_inventory", date(2026, 8, 20), date(2026, 8, 20))
        self._inventory("supplement_latest_inventory", date(2026, 8, 1), date(2026, 8, 8))

        sync_service._apply_retention(self.cursor, date(2026, 8, 20))

        self.cursor.execute("""
            SELECT stay_date FROM functions.supplement_snapshot_inventory
            ORDER BY stay_date
        """)
        surviving = [row["stay_date"] for row in self.cursor.fetchall()]
        self.assertEqual(surviving, [date(2026, 8, 20)])
        # And 1 August survives in coverage regardless, which is the whole point.
        self.assertEqual(self._coverage()["minimum_stay_date"], date(2026, 8, 1))


if __name__ == "__main__":
    unittest.main()
