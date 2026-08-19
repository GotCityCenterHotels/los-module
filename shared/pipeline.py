from datetime import datetime, timezone

from .sql_runner import transfer_dataset
from services.cost_publication_service import advance_cost_publication


def utc_now():
    return datetime.now(timezone.utc)


DATASETS = {
    "properties": {
        "export_sql": "export/cost_properties.sql",
        "import_sql": "import/upsert_cost_properties.sql",
    },
    "parking": {
        "export_sql": "export/parking_data.sql",
        "import_sql": "import/upsert_parking_data.sql",
    },
    "room_revenue": {
        "export_sql": "export/room_revenue_data.sql",
        "import_sql": "import/upsert_room_revenue_data.sql",
    },
    "total_payment": {
        "export_sql": "export/total_payment_data.sql",
        "import_sql": "import/upsert_total_payment_data.sql",
    },
    "arr_dep": {
        "export_sql": "export/arr_dep_data.sql",
        "import_sql": "import/upsert_arr_dep_data.sql",
    },
    "breakfast": {
        "export_sql": "export/breakfast_data.sql",
        "import_sql": "import/upsert_breakfast_data.sql",
    },
    # The cleaning night allocation and distribution mix, last because they are
    # the two most
    # expensive statements here and because everything above them is what the
    # statement's totals come from. Their export SQL is built at run time from
    # information_schema rather than read from a file - the Mews mirror's naming
    # for origin, travel agency, rate, room category and guest counts is not
    # knowable from this repository, and a guess that misses has to skip the
    # dataset rather than fail the import.
    "departure_mix": {
        "export_builder": "departure_mix",
        "import_sql": "import/upsert_departure_mix_data.sql",
    },
    "distribution_mix": {
        "export_builder": "distribution_mix",
        "import_sql": "import/upsert_distribution_mix_data.sql",
    },
    # Published last because it is a read model over the complete cost source
    # lifecycle. HTTP requests only read this Database A snapshot; the expensive
    # integration_db aggregation belongs in the existing background import job.
    "spit": {
        "runner": "cost_spit",
    },
}


def run_dataset(dataset_name):
    if dataset_name not in DATASETS:
        raise ValueError(
            f"Unknown dataset '{dataset_name}'. Allowed: {sorted(DATASETS)}"
        )

    # Every dataset writes into the functions schema in Database A, so the
    # target must exist before any of them run - not just "properties". The five
    # fact tables are created by migration 010; without this the other datasets
    # fail with UndefinedTable on a rebuilt database.
    from services.cost_schema_service import ensure_cost_settings_schema

    ensure_cost_settings_schema()

    config = DATASETS[dataset_name]

    if config.get("runner") == "cost_spit":
        from services.cost_spit_sync_service import sync_cost_spit

        result = sync_cost_spit()
    elif "export_builder" in config:
        from services.cost_mix_export_service import build_mix_export

        builder_name = config["export_builder"]
        result = transfer_dataset(
            export_sql_builder=lambda source: build_mix_export(builder_name, source),
            import_sql_file=config["import_sql"],
            # Mix rows are narrow and there are far more of them than in any
            # other dataset, so a smaller batch keeps each import transaction
            # short rather than holding one open across thousands of upserts.
            batch_size=2000,
            name=dataset_name,
        )
    else:
        result = transfer_dataset(
            export_sql_file=config["export_sql"],
            import_sql_file=config["import_sql"],
            batch_size=5000,
        )

    if dataset_name == "properties":
        if result["export_rows"] == 0:
            raise RuntimeError(
                "enterprise_current returned no GCCH properties from Database B"
            )

        # Verify through the same pool used by the settings API. This catches
        # configuration drift where the importer writes to a different
        # Database A than the page reads.
        from cost_database import cost_pool

        with cost_pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM functions.hotels
                    WHERE tenant_key = 'GCCH'
                    """
                )
                verified_rows = cursor.fetchone()[0]
        if verified_rows == 0:
            raise RuntimeError(
                "Properties were exported but are not visible in the unified hotel dimension in Database A"
            )
        result["verified_rows"] = verified_rows

    # transfer_dataset has committed every batch and, for mixes, the stale-row
    # prune before it returns. Move the Database A publication only after that
    # complete dataset is visible. A skipped source capability writes nothing
    # and therefore keeps the previous version.
    if result.get("import_rows", 0) or result.get("pruned_rows", 0):
        advance_cost_publication(f"import:{dataset_name}")

    return {
        "dataset": dataset_name,
        **result,
    }


def run_all_datasets():
    results = []

    for dataset_name in DATASETS:
        started_at = utc_now()

        try:
            result = run_dataset(dataset_name)
            finished_at = utc_now()

            results.append(
                {
                    "status": "success",
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "duration_seconds": (finished_at - started_at).total_seconds(),
                    **result,
                }
            )

        except Exception as exc:
            finished_at = utc_now()

            results.append(
                {
                    "status": "failed",
                    "dataset": dataset_name,
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "duration_seconds": (finished_at - started_at).total_seconds(),
                    "error": str(exc),
                }
            )

    return {
        "status": (
            "success"
            if all(result["status"] == "success" for result in results)
            else "partial_failure"
        ),
        "results": results,
    }
