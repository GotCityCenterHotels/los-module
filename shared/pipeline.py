from .sql_runner import fetch_export_rows, import_rows, transfer_dataset
from datetime import datetime, timezone

def utc_now():
    return datetime.now(timezone.utc)



DATASETS = {
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
    }
}

def run_dataset(dataset_name):
    if dataset_name not in DATASETS:
        raise ValueError(
            f"Unknown dataset '{dataset_name}'. Allowed: {sorted(DATASETS)}"
        )

    config = DATASETS[dataset_name]

    result = transfer_dataset(
        export_sql_file=config["export_sql"],
        import_sql_file=config["import_sql"],
        batch_size=5000,
    )

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