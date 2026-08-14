from datetime import datetime, timezone

from .sql_runner import transfer_dataset


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
}


def run_dataset(dataset_name):
    if dataset_name not in DATASETS:
        raise ValueError(
            f"Unknown dataset '{dataset_name}'. Allowed: {sorted(DATASETS)}"
        )

    if dataset_name == "properties":
        # The source row lives in Database B, but the import target is the
        # functions schema in Database A. Ensure that target exists before the
        # first scheduled/manual sync runs.
        from services.cost_schema_service import ensure_cost_settings_schema

        ensure_cost_settings_schema()

    config = DATASETS[dataset_name]

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
