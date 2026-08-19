"""Calendar alignment shared by Cost Data reads and background snapshots."""

from datetime import timedelta


def shift_cost_comparison_date(value, basis):
    if basis == "sameWeekday":
        return value - timedelta(days=364)
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        # 29 February compares with the last valid day of February, matching
        # LosFormat.lastYearDate in the browser.
        return value.replace(year=value.year - 1, day=28)
