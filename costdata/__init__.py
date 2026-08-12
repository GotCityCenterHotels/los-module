import json
import logging
import os
import sys
import traceback

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("InsertData function started.")

    try:
        # Make the function folder importable, e.g. InsertData/utils/...
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        from shared.pipeline import run_all_datasets, run_dataset

        try:
            body = req.get_json()
        except ValueError:
            body = {}

        dataset = body.get("dataset", "all")

        if dataset == "all":
            result = run_all_datasets()
        else:
            result = {
                "status": "success",
                "results": [
                    run_dataset(dataset)
                ],
            }

        status_code = 200
        if result.get("status") != "success":
            status_code = 207

        return func.HttpResponse(
            body=json.dumps(result, indent=2, default=str),
            status_code=status_code,
            mimetype="application/json",
        )

    except Exception as exc:
        logging.exception("InsertData function failed.")

        return func.HttpResponse(
            body=json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
                indent=2,
            ),
            status_code=500,
            mimetype="application/json",
        )