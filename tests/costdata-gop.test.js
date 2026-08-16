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
        statement.flags.some((message) => /no per-category or per-occupancy/.test(message)),
        true,
        "the blended cleaning rate must be flagged"
    );
});

// Cost Input now has a real franchise % field, so the caveat that used to
// apologise for its absence must not survive - it would keep telling operators
// to configure something they already have.
test("the missing-franchise-field caveat is gone now that the field exists", () => {
    const statement = CostData.calculateGop(data, { settingsByHotel: settings });

    assert.equal(
        statement.flags.some((message) => /no dedicated franchise/.test(message)),
        false
    );
});

function withProfile(overrides) {
    return { A: { ...settings.A, profile: { ...settings.A.profile, ...overrides } } };
}

test("a disabled franchise costs nothing even with a percentage saved", () => {
    const off = CostData.calculateGop(data, {
        settingsByHotel: withProfile({ franchiseEnabled: false, franchisePercent: "8" })
    });

    // Card 2% of 20000 plus 5% room rent on 16000, exactly as before.
    assert.equal(lineFor(off, "franchiseCardCost"), 400 + 800);
});

test("an enabled franchise is charged on the configured revenue base", () => {
    const inclProducts = CostData.calculateGop(data, {
        settingsByHotel: withProfile({
            franchiseEnabled: true,
            franchisePercent: "10",
            franchiseBasis: "net",
            franchiseRevenueBase: "roomInclProducts"
        })
    });
    assert.equal(lineFor(inclProducts, "franchiseCardCost"), 400 + 800 + 1600);

    // The same 10% on room revenue with the products taken out: the fixture
    // has no roomRevenueExclProducts1Net at all, so the base is zero and the
    // franchise adds nothing. That is the point - the two bases are different
    // columns, not the same number under two names.
    const exclProducts = CostData.calculateGop(data, {
        settingsByHotel: withProfile({
            franchiseEnabled: true,
            franchisePercent: "10",
            franchiseRevenueBase: "roomExclProducts"
        })
    });
    assert.equal(lineFor(exclProducts, "franchiseCardCost"), 400 + 800);
});

test("a gross franchise basis grosses the net revenue up by the VAT rate", () => {
    const statement = CostData.calculateGop(data, {
        settingsByHotel: withProfile({
            franchiseEnabled: true,
            franchisePercent: "10",
            franchiseBasis: "gross",
            franchiseVatPercent: "12",
            franchiseRevenueBase: "roomInclProducts"
        })
    });

    // 16000 net grossed to 17920, then 10% of that.
    assert.equal(lineFor(statement, "franchiseCardCost"), 400 + 800 + 1792);
    assert.equal(statement.flags.some((message) => /grossed up by 12%/.test(message)), true);
});

test("switching arrival cost off zeroes the line and drops its warnings", () => {
    const statement = CostData.calculateGop(data, {
        settingsByHotel: {
            A: { ...settings.A, arrivalTiers: [], profile: {
                ...settings.A.profile, arrivalCostEnabled: false
            } }
        }
    });

    assert.equal(lineFor(statement, "arrivalCost"), 0);
    assert.equal(
        statement.flags.some((message) => /reception staffing thresholds/.test(message)),
        false,
        "an intentional off switch is not a configuration gap"
    );
});

// A count above the top band used to contribute no staff hours at all and
// raise a warning nobody could act on. The top band is what the property meant.
test("a count above every threshold falls back to the highest band", () => {
    const busy = {
        ...data,
        arrivalsDepartures: [
            { stayDate: "2026-01-02", hotelName: "A", totalArrivals: 40, totalDepartures: 30 },
            { stayDate: "2026-01-03", hotelName: "A", totalArrivals: 10, totalDepartures: 20 }
        ]
    };
    const capped = {
        A: {
            ...settings.A,
            arrivalTiers: [
                { minArrivals: 0, maxArrivals: 20, receptionHours: "8" },
                { minArrivals: 21, maxArrivals: 30, receptionHours: "16" }
            ]
        }
    };
    const statement = CostData.calculateGop(busy, { settingsByHotel: capped });

    // 40 arrivals clears the closed 21-30 band, so that band's 16h applies;
    // 10 arrivals still lands in 0-20 for 8h. 24h x 300.
    assert.equal(lineFor(statement, "arrivalCost"), 7200);
    assert.equal(
        statement.flags.some((message) => /outside every configured threshold/.test(message)),
        false
    );
});

test("a count below every threshold falls back to the lowest band", () => {
    const tiers = [
        { minGuests: 50, maxGuests: 100, staffHours: "4" },
        { minGuests: 101, maxGuests: null, staffHours: "9" }
    ];

    assert.equal(CostData.matchTier(tiers, 10, "minGuests", "maxGuests").staffHours, "4");
    assert.equal(CostData.matchTier(tiers, 75, "minGuests", "maxGuests").staffHours, "4");
    assert.equal(CostData.matchTier(tiers, 400, "minGuests", "maxGuests").staffHours, "9");
    assert.equal(CostData.matchTier([], 10, "minGuests", "maxGuests"), null);
});

// A checkbox that has never been saved, a real boolean and a form-encoded
// string all reach this code. "false" is a truthy string, which is the whole
// reason this helper exists.
test("an unsaved arrival toggle defaults to on, and the string false does not", () => {
    assert.equal(CostData.isEnabled(undefined), true);
    assert.equal(CostData.isEnabled(""), true);
    assert.equal(CostData.isEnabled(false), false);
    assert.equal(CostData.isEnabled("false"), false);
    assert.equal(CostData.isEnabled("off"), false);
    assert.equal(CostData.isEnabled("true"), true);
    assert.equal(CostData.isEnabled(undefined, false), false);
});

test("periods are only computed when a grain is asked for, and sum to the total", () => {
    const withoutGrain = CostData.calculateGop(data, { settingsByHotel: settings });
    assert.deepEqual(withoutGrain.periods, []);

    const byDay = CostData.calculateGop(data, { settingsByHotel: settings, grain: "day" });
    assert.deepEqual(byDay.periods.map(({ periodKey }) => periodKey), [
        "2026-01-02", "2026-01-03"
    ]);

    // Every stay date belongs to exactly one bucket, so no revenue is counted
    // twice and none is dropped.
    const revenue = byDay.periods.reduce((total, period) => total + period.revenue, 0);
    assert.equal(revenue, lineFor(byDay, "roomRevenue") + lineFor(byDay, "parkingRevenue"));

    const byMonth = CostData.calculateGop(data, { settingsByHotel: settings, grain: "month" });
    assert.equal(byMonth.periods.length, 1);
    assert.equal(byMonth.periods[0].periodKey, "2026-01-01");
    assert.equal(byMonth.periods[0].gop, byMonth.gop);
});

test("a period's own thresholds are still evaluated per stay date", () => {
    const byMonth = CostData.calculateGop(data, { settingsByHotel: settings, grain: "month" });

    // The whole period is one bucket, but 60 and 20 guests must still band
    // separately: banding the 80-guest total would give 10h, not 14h.
    assert.equal(byMonth.periods[0].amounts.breakfastCost, 80 * 40 + 14 * 250);
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
