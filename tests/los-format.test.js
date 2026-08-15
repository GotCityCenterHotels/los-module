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
