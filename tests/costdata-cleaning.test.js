const test = require("node:test");
const assert = require("node:assert/strict");

const CostCleaning = require("../frontend/costdata-cleaning.js");

// The property's beds, defined once with the linen cost of making each up.
const BED_TYPES = [
    {bedKey: "bed-1", bedName: "Double bed", linenCost: "75"},
    {bedKey: "bed-2", bedName: "Single bed", linenCost: "45"},
    {bedKey: "bed-3", bedName: "Extra bed", linenCost: "40"},
    {bedKey: "bed-4", bedName: "Sofa bed", linenCost: "60"}
];

const bed = (key, name, quantity) => ({bedKey: key, bedName: name, quantity});

function row(categoryName, occupancy, options = {}) {
    return {
        categoryName,
        occupancy,
        cleaningMinutes: options.minutes ?? null,
        overridesBase: Boolean(options.overrides),
        beds: options.beds || []
    };
}

test("four bed types each price their own linen", () => {
    for (const [name, cost] of [
        ["Double bed", 75], ["Single bed", 45], ["Extra bed", 40], ["Sofa bed", 60]
    ]) {
        assert.equal(
            CostCleaning.bedsLinenCost([{bedName: name, quantity: 1}], BED_TYPES),
            cost
        );
    }
});

test("a double room made up with one double bed costs that bed's linen", () => {
    const rows = [
        row("Double", 1, {minutes: "30", beds: [bed("bed-1", "Double bed", 1)]}),
        row("Double", 2)
    ];

    // The setup lives on the lowest guest count and the rest follow it, so
    // both guest counts cost one double bed's linen.
    assert.equal(CostCleaning.resolveRow(rows, rows[0], BED_TYPES).linen, 75);
    assert.equal(CostCleaning.resolveRow(rows, rows[1], BED_TYPES).linen, 75);
});

test("a family room at four guests costs one double plus two singles", () => {
    const rows = [
        row("Family", 1, {minutes: "40", beds: [bed("bed-1", "Double bed", 1)]}),
        row("Family", 2),
        row("Family", 3),
        row("Family", 4, {
            overrides: true,
            beds: [bed("bed-1", "Double bed", 1), bed("bed-2", "Single bed", 2)]
        })
    ];
    const atFour = CostCleaning.resolveRow(rows, rows[3], BED_TYPES);

    // 75 for the double, plus 2 x 45 for the singles.
    assert.equal(atFour.linen, 165);
    assert.equal(atFour.inheritsBeds, false);
    // The counts nobody set separately still follow the lowest one.
    assert.equal(CostCleaning.resolveRow(rows, rows[1], BED_TYPES).linen, 75);
    assert.equal(CostCleaning.resolveRow(rows, rows[2], BED_TYPES).linen, 75);
});

test("quantity multiplies, and mixed beds add up", () => {
    assert.equal(
        CostCleaning.bedsLinenCost([
            bed("bed-1", "Double bed", 1),
            bed("bed-2", "Single bed", 2),
            bed("bed-3", "Extra bed", 3)
        ], BED_TYPES),
        75 + 90 + 120
    );
});

test("the whole setup can live on the lowest count and cover every count", () => {
    // The other way to configure the family room: put both bed types on the
    // base row, and no count needs an override at all.
    const rows = [
        row("Family", 1, {
            minutes: "40",
            beds: [bed("bed-1", "Double bed", 1), bed("bed-2", "Single bed", 2)]
        }),
        row("Family", 2),
        row("Family", 4)
    ];

    for (const each of rows) {
        assert.equal(CostCleaning.resolveRow(rows, each, BED_TYPES).linen, 165);
    }
});

test("a row with no beds costs no linen", () => {
    const rows = [row("Storage", 1)];
    assert.equal(CostCleaning.resolveRow(rows, rows[0], BED_TYPES).linen, 0);
});

test("the lowest guest count is the base whatever order the rows arrive in", () => {
    const rows = [
        row("Suite", 4, {beds: [bed("bed-4", "Sofa bed", 1)]}),
        row("Suite", 2, {minutes: "50", beds: [bed("bed-1", "Double bed", 1)]}),
        row("Suite", 3)
    ];

    assert.equal(CostCleaning.baseRowFor(rows, "Suite"), rows[1]);
    // Occupancy 3 inherits from 2, not from the row listed first.
    assert.equal(CostCleaning.resolveRow(rows, rows[2], BED_TYPES).linen, 75);
    // Occupancy 4 has beds of its own but no override, so it still inherits -
    // stale beds under a switched-off override must not leak into the cost.
    assert.equal(CostCleaning.resolveRow(rows, rows[0], BED_TYPES).linen, 75);
});

test("each category has its own base", () => {
    const rows = [
        row("Double", 1, {minutes: "30", beds: [bed("bed-1", "Double bed", 1)]}),
        row("Single", 1, {minutes: "20", beds: [bed("bed-2", "Single bed", 1)]}),
        row("Single", 2)
    ];

    assert.equal(CostCleaning.resolveRow(rows, rows[2], BED_TYPES).linen, 45);
});

test("minutes inherit when blank and stand when typed", () => {
    const rows = [
        row("Double", 1, {minutes: "30", beds: [bed("bed-1", "Double bed", 1)]}),
        row("Double", 2),
        row("Double", 3, {minutes: "45"})
    ];

    assert.equal(CostCleaning.resolveRow(rows, rows[1], BED_TYPES).minutes, "30");
    assert.equal(CostCleaning.resolveRow(rows, rows[1], BED_TYPES).inheritsMinutes, true);
    assert.equal(CostCleaning.resolveRow(rows, rows[2], BED_TYPES).minutes, "45");
    assert.equal(CostCleaning.resolveRow(rows, rows[2], BED_TYPES).inheritsMinutes, false);
    // Zero is a number someone typed, not a blank.
    const zeroed = [rows[0], row("Double", 2, {minutes: "0"})];
    assert.equal(CostCleaning.resolveRow(zeroed, zeroed[1], BED_TYPES).minutes, "0");
});

test("a bed is followed by key, so a renamed bed keeps its rooms and its price", () => {
    const renamed = [{bedKey: "bed-1", bedName: "Kingsize", linenCost: "75"}];
    const rows = [row("Double", 1, {beds: [bed("bed-1", "Double bed", 1)]})];

    // The row still carries the old name; the key is what binds it.
    const resolved = CostCleaning.resolveRowBed(rows[0].beds[0], renamed);
    assert.equal(resolved.name, "Kingsize");
    assert.equal(CostCleaning.resolveRow(rows, rows[0], renamed).linen, 75);
});

test("a model straight from the database resolves by name, before keys exist", () => {
    // Nothing persisted carries a key - the database stores names - so the
    // first resolve after a load has to work without them.
    const fromDatabase = [{bedName: "Double bed", linenCost: "75"}];
    const rows = [row("Double", 1, {beds: [{bedName: "Double bed", quantity: 1}]})];

    assert.equal(CostCleaning.resolveRow(rows, rows[0], fromDatabase).linen, 75);
});

test("a bed type that no longer exists costs nothing and keeps its name visible", () => {
    const rows = [row("Double", 1, {beds: [{bedName: "Waterbed", quantity: 1}]})];
    const resolved = CostCleaning.resolveRowBed(rows[0].beds[0], BED_TYPES);

    assert.equal(resolved.bed, null);
    assert.equal(resolved.name, "Waterbed");
    assert.equal(CostCleaning.resolveRow(rows, rows[0], BED_TYPES).linen, 0);
});

test("a missing or unusable quantity counts as one bed", () => {
    for (const quantity of [undefined, null, "", 0, -3, "abc"]) {
        assert.equal(
            CostCleaning.bedsLinenCost(
                [{bedKey: "bed-1", bedName: "Double bed", quantity}], BED_TYPES
            ),
            75,
            `quantity ${JSON.stringify(quantity)} should count as one bed`
        );
    }
});
