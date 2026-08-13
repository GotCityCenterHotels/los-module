const test = require("node:test");
const assert = require("node:assert/strict");

const SupplementData = require("../frontend/supplement-data.js");

const range = { startDate: "2026-08-10", endDate: "2026-08-16", today: "2026-08-13" };

test("supplement mock generation is deterministic", () => {
    const first = SupplementData.generateDataset({ ...range, lyComparisonType: "sameDate" });
    const second = SupplementData.generateDataset({ ...range, lyComparisonType: "sameDate" });
    assert.deepEqual(first, second);
    assert.equal(first.dates.length, 7);
    assert.equal(first.records.length, SupplementData.HOTELS.length * SupplementData.CATEGORIES.length);
});

test("date range validation rejects invalid and oversized ranges", () => {
    assert.equal(SupplementData.validateDateRange("2026-08-12", "2026-08-10").valid, false);
    assert.equal(SupplementData.validateDateRange("2026-08-01", "2026-08-31").valid, true);
    const tooLarge = SupplementData.validateDateRange("2026-08-01", "2026-09-01");
    assert.equal(tooLarge.valid, false);
    assert.match(tooLarge.error, /31 days/);
});

test("same weekday comparison is 364 days earlier", () => {
    const source = SupplementData.parseDateKey("2026-08-13");
    assert.equal(SupplementData.formatDateKey(SupplementData.getComparisonDate(source, "sameWeekday")), "2025-08-14");
    assert.equal(SupplementData.getComparisonDate(source, "sameWeekday").getUTCDay(), source.getUTCDay());
});

test("same date comparison clamps leap day to the prior February", () => {
    const leapDay = SupplementData.parseDateKey("2024-02-29");
    assert.equal(SupplementData.formatDateKey(SupplementData.getComparisonDate(leapDay, "sameDate")), "2023-02-28");
});

test("single and comparison modes respect visible rows and append totals", () => {
    const dataset = SupplementData.generateDataset({ ...range, lyComparisonType: "sameDate" });
    const single = SupplementData.buildRows(dataset, {
        mode: "single",
        hotelCode: "hotel-vasa",
        enabledCategories: ["standard", "suite"]
    });
    assert.deepEqual(single.map(({ code }) => code), ["standard", "suite", "total"]);

    const comparison = SupplementData.buildRows(dataset, {
        mode: "comparison",
        enabledHotels: ["hotel-a", "hotel-vasa"]
    });
    assert.deepEqual(comparison.map(({ code }) => code), ["hotel-a", "hotel-vasa", "total"]);
});

test("total metrics are recomputed from summed facts", () => {
    const dataset = SupplementData.generateDataset({ ...range, lyComparisonType: "sameDate" });
    const rows = SupplementData.buildRows(dataset, {
        mode: "single",
        hotelCode: "hotel-a",
        enabledCategories: SupplementData.CATEGORIES.map(({ code }) => code)
    });
    const total = rows.at(-1).cells[0].today;
    const components = rows.slice(0, -1).map((row) => row.cells[0].today);
    assert.equal(total.inventory, components.reduce((sum, item) => sum + item.inventory, 0));
    assert.equal(total.roomsSold, components.reduce((sum, item) => sum + item.roomsSold, 0));
    assert.equal(total.metrics.occ, total.roomsSold / total.inventory * 100);
});

test("averages, differences, and metric formatting are stable", () => {
    const dataset = SupplementData.generateDataset({ ...range, lyComparisonType: "sameDate" });
    const row = SupplementData.buildRows(dataset, { mode: "single", hotelCode: "hotel-a" })[0];
    const averages = SupplementData.computeRowAverages(row);
    assert.ok(Number.isFinite(averages.today.occ));
    const periodRooms = row.cells.reduce((sum, cell) => sum + cell.today.roomsSold, 0);
    const periodInventory = row.cells.reduce((sum, cell) => sum + cell.today.inventory, 0);
    assert.equal(averages.today.occ, periodRooms / periodInventory * 100);
    assert.equal(SupplementData.calculateDifference(120, 100, "percent"), 20);
    assert.equal(SupplementData.calculateDifference(120, 100, "currency"), 20);
    assert.equal(SupplementData.formatDifference(4.25, "percent", "adr"), "+4.3%");
    assert.equal(SupplementData.formatDifference(-12, "currency", "adr"), "-12 kr");
    assert.match(SupplementData.formatMetric(82.54, "occ"), /^82\.5%$/);
});

test("detail data contains a complete synthetic breakdown and pickup comparison", () => {
    const detail = SupplementData.getDetailData({
        hotelCode: "hotel-vasa",
        categoryCode: "suite",
        date: "2026-08-14",
        metric: "adr"
    });
    assert.equal(detail.hotel.name, "Hotel Vasa");
    assert.equal(detail.category.name, "Suite");
    assert.equal(detail.breakdown.length, 4);
    assert.equal(detail.curve.length, 13);
    assert.ok(detail.curve.every((point) => Number.isFinite(point.today) && Number.isFinite(point.comparison)));
});
