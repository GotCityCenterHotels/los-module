import argparse

from datetime import date

from services.supplement_sync_service import run_backfill_partition


def main():
    parser = argparse.ArgumentParser(
        description="Copy one integration_db Supplement snapshot into PostgreSQL."
    )
    parser.add_argument("snapshot_date", type=date.fromisoformat)
    arguments = parser.parse_args()
    result = run_backfill_partition(arguments.snapshot_date)
    print(result)


if __name__ == "__main__":
    main()
