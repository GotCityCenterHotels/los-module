const test = require("node:test");
const assert = require("node:assert/strict");

const SupplementData = require("../frontend/supplement-data.js");

test("date validation permits 366 days and rejects larger ranges", () => {
    assert.equal(SupplementData.validateDateRange("2026-08-12", "2026-08-10").valid, false);
    assert.equal(SupplementData.validateDateRange("2024-01-01", "2024-12-31").valid, true);
    const tooLarge = SupplementData.validateDateRange("2024-01-01", "2025-01-01");
    assert.equal(tooLarge.valid, false);
    assert.match(tooLarge.error, /366 days/);
});

test("weighted period averages use additive facts", () => {
    const row = {
        cells: [
            { today: { assignedRooms: 10, revenue: 10000, inventory: 20 }, spit: {}, ly: {} },
            { today: { assignedRooms: 90, revenue: 180000, inventory: 100 }, spit: {}, ly: {} }
        ]
    };
    const averages = SupplementData.computeRowAverages(row);
    assert.equal(averages.today.occ, 100 / 120 * 100);
    assert.equal(averages.today.adr, 190000 / 100);
    assert.equal(averages.today.revpar, 190000 / 120);
});

test("a subtotal of published rows equals the row the server would publish", () => {
    // Switching a room category off is a local filter over the full grid, so the
    // subtotal it produces has to be the same weighted figure the server derives
    // when it is asked for that subset: sums re-divided, never an average of
    // averages.
    const rowA = {
        cells: [
            { today: { assignedRooms: 10, revenue: 10000, inventory: 20 }, spit: {}, ly: {} },
            { today: { assignedRooms: 4, revenue: 6000, inventory: 20 }, spit: {}, ly: {} }
        ]
    };
    const rowB = {
        cells: [
            { today: { assignedRooms: 90, revenue: 180000, inventory: 100 }, spit: {}, ly: {} },
            { today: { assignedRooms: 50, revenue: 125000, inventory: 100 }, spit: {}, ly: {} }
        ]
    };

    const cells = SupplementData.sumRowCells([rowA, rowB], 2);
    assert.equal(cells.length, 2);
    assert.equal(cells[0].today.assignedRooms, 100);
    assert.equal(cells[0].today.revenue, 190000);
    assert.equal(cells[0].today.inventory, 120);
    assert.equal(cells[0].today.occ, 100 / 120 * 100);
    assert.equal(cells[0].today.adr, 190000 / 100);
    assert.equal(cells[0].today.revpar, 190000 / 120);
    assert.equal(cells[1].today.adr, 131000 / 54);

    // And the period average over the subtotal matches averaging the parts.
    const subtotalAverages = SupplementData.computeRowAverages({ cells });
    assert.equal(subtotalAverages.today.adr, (190000 + 131000) / (100 + 54));
    assert.equal(subtotalAverages.today.occ, (100 + 54) / (120 + 120) * 100);
});

test("an absent mode on a cell contributes nothing rather than NaN", () => {
    const cells = SupplementData.sumRowCells(
        [{ cells: [{ today: { assignedRooms: 5, revenue: 500, inventory: 10 } }] }],
        1
    );
    assert.equal(cells[0].ly.assignedRooms, 0);
    assert.equal(cells[0].ly.occ, null);
    assert.equal(cells[0].ly.adr, null);
    assert.equal(cells[0].today.occ, 50);
});

test("differences and metric formatting remain stable", () => {
    assert.equal(SupplementData.calculateDifference(120, 100, "percent"), 20);
    assert.equal(SupplementData.calculateDifference(120, 100, "currency"), 20);
    assert.equal(SupplementData.formatDifference(4.25, "percent", "adr"), "+4.3%");
    assert.equal(SupplementData.formatDifference(-12, "currency", "adr"), "-12 kr");
    assert.equal(SupplementData.formatMetric(82.54, "occ"), "82.5%");
});

test("production browser data module contains API clients and no mock generator", () => {
    assert.equal(typeof SupplementData.fetchMetadata, "function");
    assert.equal(typeof SupplementData.fetchGrid, "function");
    assert.equal(typeof SupplementData.fetchDetail, "function");
    assert.equal(SupplementData.generateDataset, undefined);
});
