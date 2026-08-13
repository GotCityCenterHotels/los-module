import argparse
import gzip
import json
import time

from datetime import date, timedelta

from services import supplement_service


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark an uncached 366-day all-hotel Supplement grid from Database A."
    )
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    arguments = parser.parse_args()
    start_date = arguments.end_date - timedelta(days=365)

    metadata = supplement_service.list_supplement_hotels()
    hotel_codes = [hotel["code"] for hotel in metadata["hotels"]]
    supplement_service._grid_cache.clear()
    started_at = time.perf_counter()
    payload = supplement_service.fetch_supplement_grid(
        start_date,
        arguments.end_date,
        mode="comparison",
        hotel_codes=hotel_codes,
    )
    elapsed_seconds = time.perf_counter() - started_at
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(encoded, compresslevel=5)
    result = {
        "days": len(payload["dates"]),
        "hotels": len(hotel_codes),
        "uncachedSeconds": round(elapsed_seconds, 3),
        "jsonBytes": len(encoded),
        "gzipBytes": len(compressed),
        "targets": {"uncachedSeconds": 2, "gzipBytes": 5_000_000},
        "passed": elapsed_seconds < 2 and len(compressed) < 5_000_000,
    }
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
