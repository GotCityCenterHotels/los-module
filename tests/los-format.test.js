const test = require("node:test");
const assert = require("node:assert/strict");

const LosFormat = require("../frontend/los-format.js");

// Intl groups with a narrow no-break space, which is invisible in a diff.
const spaced = (value) => value.replace(/[\s  ]/g, " ");

test("month buckets are labelled MMM YYYY, not as a day", () => {
    // The bug this guards: a month bucket key is the first of the month, so
    // formatting it as a date produced "1 Jan 2026" for a whole month.
    assert.equal(LosFormat.periodLabel("2026-01-01", "month"), "Jan 2026");
    assert.equal(LosFormat.periodLabel("2026-02-01", "month"), "Feb 2026");
    assert.equal(LosFormat.periodLabel("2026-12-01", "month"), "Dec 2026");
});

test("every grain produces its own label from the same key", () => {
    assert.equal(LosFormat.periodLabel("2026-01-01", "day"), "1 Jan 2026");
    assert.equal(LosFormat.periodLabel("2026-01-01", "year"), "2026");
    // 2025-12-29 is the Monday of ISO week 1 of 2026.
    assert.equal(LosFormat.periodLabel("2025-12-29", "week"), "Wk 01 2026");
    assert.equal(LosFormat.periodLabel("2026-01-05", "week"), "Wk 02 2026");
});

test("ISO weeks belong to the year holding their Thursday", () => {
    assert.deepEqual(LosFormat.isoWeek("2025-12-29"), { week: 1, weekYear: "2026" });
    assert.deepEqual(LosFormat.isoWeek("2026-12-28"), { week: 53, weekYear: "2026" });
});

test("non-date period keys pass through unchanged", () => {
    assert.equal(LosFormat.periodLabel("All", "month"), "All");
    assert.equal(LosFormat.periodLabel("", "day"), "");
});

test("chart labels band the year only where the label lacks one", () => {
    // Month and year carry their own year, so repeating it under the tick
    // would print "Jan 2026" twice.
    assert.deepEqual(LosFormat.periodLabelParts("2026-03-01", "month"), {
        primary: "Mar 2026", year: null
    });
    assert.deepEqual(LosFormat.periodLabelParts("2026-01-01", "year"), {
        primary: "2026", year: null
    });
    assert.deepEqual(LosFormat.periodLabelParts("2026-03-09", "day"), {
        primary: "09 Mar", year: "2026"
    });
    assert.deepEqual(LosFormat.periodLabelParts("2026-03-09", "week"), {
        primary: "Wk 11", year: "2026"
    });
});

test("SEK is whole kronor with no decimal part", () => {
    assert.equal(spaced(LosFormat.formatSek(1234.56)), "1 235");
    assert.equal(spaced(LosFormat.formatSek("1234.4")), "1 234");
    assert.equal(spaced(LosFormat.formatSek(0)), "0");
    assert.equal(spaced(LosFormat.formatSekAmount(1234.56)), "1 235 kr");
    assert.match(LosFormat.formatSek(1234.56), /^\D?1\D?235$/u);
});

test("negative amounts round away from zero on the half", () => {
    // Math.round alone turns -0.5 into -0 and -1.5 into -1, which makes a
    // credit and a charge of the same size round differently.
    assert.equal(LosFormat.roundSek(-1.5), -2);
    assert.equal(LosFormat.roundSek(1.5), 2);
    assert.equal(LosFormat.roundSek(-0.5), -1);
});

test("unusable values format as an em dash rather than zero", () => {
    assert.equal(LosFormat.roundSek(null), null);
    assert.equal(LosFormat.roundSek("abc"), null);
    assert.equal(LosFormat.formatSek(undefined), "—");
});

test("an empty money input stays empty instead of becoming zero", () => {
    assert.equal(LosFormat.normalizeSekInputValue(""), "");
    assert.equal(LosFormat.normalizeSekInputValue(null), "");
    assert.equal(LosFormat.normalizeSekInputValue("1499.75"), "1500");
});

test("the money field registry is the single list of SEK inputs", () => {
    assert.equal(LosFormat.isMoneyField("linenCost"), true);
    assert.equal(LosFormat.isMoneyField("breakfastStaffCostPerHour"), true);
    // Hours, minutes and percentages are not money and keep their decimals.
    assert.equal(LosFormat.isMoneyField("staffHours"), false);
    assert.equal(LosFormat.isMoneyField("cleaningMinutes"), false);
    assert.equal(LosFormat.isMoneyField("cardCostPercent"), false);
});

// ---------------------------------------------------------------------------
// Last year
//
// Two bases, the same two the LOS API accepts. Both are easy to get subtly wrong
// in a way no total on the Cost Data page would reveal: a comparison that is off
// by a day simply reads as a bad month.
// ---------------------------------------------------------------------------

const weekdayOf = (iso) => new Date(`${iso}T00:00:00Z`).getUTCDay();

test("same date holds the calendar date and same weekday holds the weekday", () => {
    assert.equal(LosFormat.lastYearDate("2026-03-01", "sameDate"), "2025-03-01");

    // 364 days is 52 whole weeks, so this is the nearest date a year back that
    // falls on the same day of the week - which is the whole point of the basis.
    const weekday = LosFormat.lastYearDate("2026-03-01", "sameWeekday");
    assert.equal(weekday, "2025-03-02");
    assert.equal(weekdayOf(weekday), weekdayOf("2026-03-01"));
    assert.equal(LosFormat.LY_WEEKDAY_OFFSET_DAYS, 364);
});

test("an unrecognised basis is treated as same date, never as no shift", () => {
    // A typo in the select's value must not silently compare a period with
    // itself, which would draw two identical bars and read as no change at all.
    assert.equal(LosFormat.lastYearDate("2026-03-01", ""), "2025-03-01");
    assert.equal(LosFormat.lastYearDate("2026-03-01", undefined), "2025-03-01");
});

test("last year's date and this year's are inverses on ordinary dates", () => {
    for (const basis of ["sameDate", "sameWeekday"]) {
        for (const date of ["2026-01-01", "2026-06-15", "2026-12-31"]) {
            assert.equal(
                LosFormat.thisYearDate(LosFormat.lastYearDate(date, basis), basis),
                date,
                `${date} on ${basis}`
            );
        }
    }
});

test("29 February lands on the 28th rather than rolling into March", () => {
    // Date arithmetic rolls it onto 1 March, which at a month grain moves a day
    // into the wrong bar. Both of a leap year's late-February days compare
    // against the 28th instead, so the comparison keeps every krona.
    assert.equal(LosFormat.thisYearDate("2024-02-29", "sameDate"), "2025-02-28");
    assert.equal(LosFormat.lastYearDate("2024-02-29", "sameDate"), "2023-02-28");
    // Leap to leap is untouched.
    assert.equal(LosFormat.lastYearDate("2024-02-29", "sameDate").slice(0, 4), "2023");
    assert.equal(LosFormat.thisYearDate("2023-02-28", "sameDate"), "2024-02-28");
    // Nothing else in February moves.
    assert.equal(LosFormat.thisYearDate("2024-02-28", "sameDate"), "2025-02-28");
});

test("a range shifts both ends, keeping its length on a weekday basis", () => {
    const range = { startDate: "2026-01-01", endDate: "2026-08-17" };

    assert.deepEqual(LosFormat.lastYearRange(range, "sameDate"), {
        startDate: "2025-01-01", endDate: "2025-08-17"
    });
    // 364 days off both ends is the same number of days, and both ends keep
    // their weekday - so the comparison covers the same shape of week.
    const weekday = LosFormat.lastYearRange(range, "sameWeekday");
    assert.equal(weekday.startDate, "2025-01-02");
    assert.equal(weekdayOf(weekday.startDate), weekdayOf(range.startDate));
    assert.equal(weekdayOf(weekday.endDate), weekdayOf(range.endDate));
});

test("period keys shift as dates, so a shifted key still labels its own period", () => {
    // Month buckets are keyed by their first date and week buckets by a Monday.
    // 364 days is a whole number of weeks, so a Monday stays a Monday.
    assert.equal(LosFormat.periodLabel(
        LosFormat.lastYearDate("2026-03-01", "sameDate"), "month"
    ), "Mar 2025");
    const monday = LosFormat.lastYearDate("2026-03-02", "sameWeekday");
    assert.equal(weekdayOf(monday), 1);
    assert.equal(LosFormat.isoWeek(monday).week, LosFormat.isoWeek("2026-03-02").week);
});

test("a value that is not an ISO date is handed back rather than mangled", () => {
    assert.equal(LosFormat.lastYearDate("All", "sameDate"), "All");
    assert.equal(LosFormat.lastYearDate("", "sameWeekday"), "");
    assert.equal(LosFormat.thisYearDate(null, "sameDate"), "");
});
