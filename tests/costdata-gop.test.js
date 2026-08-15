const test = require("node:test");
const assert = require("node:assert/strict");

const CostData = require("../frontend/costdata-data.js");

// Two stay dates so the per-day staffing thresholds are actually exercised:
// a threshold that is evaluated on the period total instead of per day would
// pick a different band and produce a different cost.
const data = {
    roomRevenue: [
        { stayDate: "2026-01-02", hotelName: "A", amountCurrency: "SEK", roomRevenueInclProducts1Net: "10000" },
        { stayDate: "2026-01-03", hotelName: "A", amountCurrency: "SEK", roomRevenueInclProducts1Net: "6000" }
    ],
    payments: [
        { stayDate: "2026-01-02", hotelName: "A", amountCurrency: "SEK", totalPaymentAmountGrossValue: "12000" },
        { stayDate: "2026-01-03", hotelName: "A", amountCurrency: "SEK", totalPaymentAmountGrossValue: "8000" }
    ],
    breakfast: [
        { stayDate: "2026-01-02", hotelName: "A", breakfastTotal: 60, breakfastNetCost: "4000" },
        { stayDate: "2026-01-03", hotelName: "A", breakfastTotal: 20, breakfastNetCost: "1500" }
    ],
    parking: [
        { stayDate: "2026-01-02", hotelName: "A", service: "Garage", totalParkingAmountNetValue: "1500" },
        { stayDate: "2026-01-03", hotelName: "A", service: "Garage", totalParkingAmountNetValue: "500" }
    ],
    arrivalsDepartures: [
        { stayDate: "2026-01-02", hotelName: "A", totalArrivals: 40, totalDepartures: 30 },
        { stayDate: "2026-01-03", hotelName: "A", totalArrivals: 10, totalDepartures: 20 }
    ]
};

const settings = {
    A: {
        enterpriseId: "property-a",
        hotelName: "A",
        profile: {
            currency: "SEK",
            distributionDefaultPercent: "10",
            cleaningCostPerMinute: "5",
            receptionCostPerHour: "300",
            roomRentPercent: "5",
            breakfastCalculationBasis: "guests",
            breakfastFoodCostPerGuest: "40",
            breakfastStaffCostPerHour: "250",
            breakfastRentPercent: "0",
            parkingRentPercent: "0",
            cardCostPercent: "2"
        },
        distributionGroups: [],
        cleaningCategories: [
            { categoryName: "Double", occupancy: 1, cleaningMinutes: "20", linenCost: "50" },
            { categoryName: "Double", occupancy: 2, cleaningMinutes: "30", linenCost: "70" }
        ],
        arrivalTiers: [
            { minArrivals: 0, maxArrivals: 20, receptionHours: "8" },
            { minArrivals: 21, maxArrivals: null, receptionHours: "16" }
        ],
        breakfastTiers: [
            { minGuests: 0, maxGuests: 30, staffHours: "4" },
            { minGuests: 31, maxGuests: null, staffHours: "10" }
        ]
    }
};

function lineFor(statement, key) {
    return statement.lines.find((line) => line.key === key).amount;
}

test("the statement is exactly the eight rows, in the required order", () => {
    const statement = CostData.calculateGop(data, { settingsByHotel: settings });

    assert.deepEqual(statement.lines.map(({ label }) => label), [
        "Room revenue incl products",
        "Parking revenue",
        "Breakfast cost",
        "Distribution cost",
        "Franchise & card cost",
        "Cleaning cost",
        "Arrival cost",
        "Gross Operating Profit (GOP)"
    ]);
    assert.deepEqual(statement.lines.map(({ type }) => type), [
        "revenue", "revenue", "cost", "cost", "cost", "cost", "cost", "result"
    ]);
});

test("every cost comes from the saved cost input values", () => {
    const statement = CostData.calculateGop(data, { settingsByHotel: settings });

    assert.equal(lineFor(statement, "roomRevenue"), 16000);
    assert.equal(lineFor(statement, "parkingRevenue"), 2000);

    // Food 80 guests x 40. Staffing is per day: 60 guests hits the 31+ band
    // (10h) and 20 guests hits the 0-30 band (4h), so 14h x 250.
    assert.equal(lineFor(statement, "breakfastCost"), 80 * 40 + 14 * 250);

    // 10% of room revenue incl products.
    assert.equal(lineFor(statement, "distributionCost"), 1600);

    // Card 2% of 20000 gross payments, plus 5% room rent on 16000.
    assert.equal(lineFor(statement, "franchiseCardCost"), 400 + 800);

    // 50 departures x the mean of (20x5 + 50) and (30x5 + 70) = 185.
    assert.equal(lineFor(statement, "cleaningCost"), 50 * 185);

    // 40 arrivals -> 16h, 10 arrivals -> 8h, so 24h x 300.
    assert.equal(lineFor(statement, "arrivalCost"), 7200);
});

test("GOP is revenue minus every cost, and matches the rows shown", () => {
    const statement = CostData.calculateGop(data, { settingsByHotel: settings });
    const revenue = lineFor(statement, "roomRevenue") + lineFor(statement, "parkingRevenue");
    const costs = ["breakfastCost", "distributionCost", "franchiseCardCost", "cleaningCost", "arrivalCost"]
        .reduce((total, key) => total + lineFor(statement, key), 0);

    assert.equal(statement.gop, revenue - costs);
    assert.equal(lineFor(statement, "gop"), statement.gop);
});

test("changing a cost input value moves the dashboard figure and GOP", () => {
    const before = CostData.calculateGop(data, { settingsByHotel: settings });
    const doubled = {
        A: {
            ...settings.A,
            profile: { ...settings.A.profile, receptionCostPerHour: "600" }
        }
    };
    const after = CostData.calculateGop(data, { settingsByHotel: doubled });

    assert.equal(lineFor(after, "arrivalCost"), lineFor(before, "arrivalCost") * 2);
    assert.equal(after.gop, before.gop - lineFor(before, "arrivalCost"));
});

test("every line is whole kronor and the column still adds up", () => {
    const fractional = {
        A: {
            ...settings.A,
            profile: { ...settings.A.profile, distributionDefaultPercent: "10.333" }
        }
    };
    const statement = CostData.calculateGop(data, { settingsByHotel: fractional });

    for (const line of statement.lines) {
        assert.equal(Number.isInteger(line.amount), true, `${line.key} is not whole kronor`);
    }
    // 10.333% of 16000 is 1653.28: rounded once, and GOP derived from the
    // rounded row rather than from the unrounded total.
    assert.equal(lineFor(statement, "distributionCost"), 1653);
});

test("an unconfigured property is flagged, not costed with invented values", () => {
    const statement = CostData.calculateGop(data, { settingsByHotel: {} });

    assert.equal(lineFor(statement, "roomRevenue"), 16000);
    assert.equal(lineFor(statement, "breakfastCost"), 0);
    assert.equal(lineFor(statement, "cleaningCost"), 0);
    assert.equal(
        statement.flags.some((message) => /no Cost Input configuration/.test(message)),
        true
    );
});

test("derivations the fact data cannot support exactly are flagged", () => {
    const statement = CostData.calculateGop(data, { settingsByHotel: settings });

    assert.equal(
        statement.flags.some((message) => /no dedicated franchise/.test(message)),
        true,
        "the missing franchise % field must be flagged"
    );
    assert.equal(
        statement.flags.some((message) => /no per-category or per-occupancy/.test(message)),
        true,
        "the blended cleaning rate must be flagged"
    );
});

test("missing thresholds are reported rather than silently costing zero", () => {
    const withoutTiers = {
        A: { ...settings.A, arrivalTiers: [], breakfastTiers: [] }
    };
    const statement = CostData.calculateGop(data, { settingsByHotel: withoutTiers });

    assert.equal(lineFor(statement, "arrivalCost"), 0);
    // Food cost still applies; only the staffing half is missing.
    assert.equal(lineFor(statement, "breakfastCost"), 80 * 40);
    assert.equal(
        statement.flags.some((message) => /no reception staffing thresholds/.test(message)),
        true
    );
    assert.equal(
        statement.flags.some((message) => /no breakfast staffing thresholds/.test(message)),
        true
    );
});

test("a hotel filter narrows the statement to that property", () => {
    const twoHotels = {
        ...data,
        roomRevenue: [
            ...data.roomRevenue,
            { stayDate: "2026-01-02", hotelName: "B", amountCurrency: "SEK", roomRevenueInclProducts1Net: "9999" }
        ]
    };

    assert.equal(
        lineFor(CostData.calculateGop(twoHotels, { settingsByHotel: settings }), "roomRevenue"),
        25999
    );
    assert.equal(
        lineFor(CostData.calculateGop(twoHotels, {
            hotelName: "A", settingsByHotel: settings
        }), "roomRevenue"),
        16000
    );
});

test("revenue in another currency is excluded and reported", () => {
    const mixed = {
        ...data,
        roomRevenue: [
            ...data.roomRevenue,
            { stayDate: "2026-01-02", hotelName: "A", amountCurrency: "EUR", roomRevenueInclProducts1Net: "500" }
        ]
    };
    const statement = CostData.calculateGop(mixed, { settingsByHotel: settings });

    assert.equal(lineFor(statement, "roomRevenue"), 16000);
    assert.equal(statement.flags.some((message) => /EUR was excluded/.test(message)), true);
});
