import logging
import os
import sys
import traceback

import azure.functions as func


def main(mytimer: func.TimerRequest) -> None:
    logging.info("CostDataTimer started.")

    try:
        if mytimer.past_due:
            logging.warning("CostDataTimer is running later than scheduled.")

        wwwroot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        costdata_dir = os.path.join(wwwroot, "CostData")

        if costdata_dir not in sys.path:
            sys.path.insert(0, costdata_dir)

        from shared.pipeline import run_all_datasets

        result = run_all_datasets()

        logging.info("CostDataTimer finished. Result: %s", result)

        if result.get("status") != "success":
            raise RuntimeError(f"CostDataTimer finished with non-success status: {result}")

    except Exception:
        logging.error("CostDataTimer failed:\n%s", traceback.format_exc())
        raise