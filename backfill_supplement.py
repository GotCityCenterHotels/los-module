import argparse
import sys

from datetime import date, timedelta
from time import perf_counter, sleep

from cost_database import cost_pool
from services.supplement_sync_service import run_backfill_partition


def format_duration(seconds):
    seconds = max(0, round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def snapshot_dates(snapshot_from, snapshot_to):
    if snapshot_to < snapshot_from:
        raise ValueError("snapshot_to cannot be before snapshot_from")
    return (
        snapshot_from + timedelta(days=offset)
        for offset in range((snapshot_to - snapshot_from).days + 1)
    )


def run_backfill_range(snapshot_from, snapshot_to, pause_seconds=0, output=print):
    if pause_seconds < 0:
        raise ValueError("pause_seconds cannot be negative")
    total_dates = (snapshot_to - snapshot_from).days + 1
    if total_dates < 1:
        raise ValueError("snapshot_to cannot be before snapshot_from")

    started_at = perf_counter()
    exported_rows = 0
    imported_rows = 0
    completed_dates = 0
    current_date = snapshot_from
    output(
        f"Supplement backfill: {snapshot_from} through {snapshot_to} "
        f"({total_dates} snapshot dates, one transaction per date)"
    )

    try:
        for index, current_date in enumerate(
            snapshot_dates(snapshot_from, snapshot_to), start=1
        ):
            date_started_at = perf_counter()
            output(f"[{index}/{total_dates}] Importing {current_date} ...")
            result = run_backfill_partition(current_date)
            completed_dates += 1
            exported_rows += result["exportedRows"]
            imported_rows += result["importedRows"]
            elapsed = perf_counter() - date_started_at
            output(
                f"[{index}/{total_dates}] Published {current_date} "
                f"(run {result['runId']}, {elapsed:.1f}s, "
                f"{result['exportedRows']} source rows, "
                f"{result['importedRows']} PostgreSQL rows)"
            )
            remaining_dates = total_dates - completed_dates
            if remaining_dates:
                average_seconds = (perf_counter() - started_at) / completed_dates
                estimate = average_seconds * remaining_dates
                estimate += pause_seconds * max(0, remaining_dates - 1)
                output(f"Estimated remaining time: {format_duration(estimate)}")
            if pause_seconds and index < total_dates:
                sleep(pause_seconds)
    except (Exception, KeyboardInterrupt):
        message = (
            "Backfill stopped. Resume safely with: "
            f"python backfill_supplement.py {current_date} {snapshot_to} "
            f"--pause-seconds {pause_seconds:g}"
        )
        if output is print:
            output(message, file=sys.stderr)
        else:
            output(message)
        raise

    summary = {
        "status": "completed",
        "snapshotFrom": snapshot_from.isoformat(),
        "snapshotTo": snapshot_to.isoformat(),
        "completedDates": completed_dates,
        "exportedRows": exported_rows,
        "importedRows": imported_rows,
        "elapsedSeconds": round(perf_counter() - started_at, 1),
    }
    output(summary)
    return summary


def main():
    try:
        parser = argparse.ArgumentParser(
            description=(
                "Copy one or more integration_db Supplement snapshots into PostgreSQL, "
                "processing and publishing one date at a time."
            )
        )
        parser.add_argument("snapshot_from", type=date.fromisoformat)
        parser.add_argument(
            "snapshot_to",
            nargs="?",
            type=date.fromisoformat,
            help="Inclusive end date; omit to import only snapshot_from.",
        )
        parser.add_argument(
            "--pause-seconds",
            type=float,
            default=0,
            help="Optional delay between snapshot dates to reduce sustained source load.",
        )
        arguments = parser.parse_args()
        snapshot_to = arguments.snapshot_to or arguments.snapshot_from
        run_backfill_range(
            arguments.snapshot_from,
            snapshot_to,
            arguments.pause_seconds,
        )
    finally:
        cost_pool.close(timeout=30)


if __name__ == "__main__":
    main()
