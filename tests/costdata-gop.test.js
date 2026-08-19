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

test("the statement is exactly the nine rows, in the required order", () => {
    const statement = CostData.calculateGop(data, { settingsByHotel: settings });

    // Rent is its own line rather than part of franchise & card: a different
    // agreement, usually the larger of the two, and folding it in left the
    // statement with no rent on it at all.
    assert.deepEqual(statement.lines.map(({ label }) => label), [
        "Room revenue incl products",
        "Parking revenue",
        "Breakfast cost",
        "Distribution cost",
        "Franchise & card cost",
        "Rent cost",
        "Cleaning cost",
        "Arrival cost",
        "Gross Operating Profit (GOP)"
    ]);
    assert.deepEqual(statement.lines.map(({ type }) => type), [
        "revenue", "revenue", "cost", "cost", "cost", "cost", "cost", "cost", "result"
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

    // Card 2% of 20000 gross payments. Franchise is off in the fixture.
    assert.equal(lineFor(statement, "franchiseCardCost"), 400);
    // 5% room rent on 16000, on its own line.
    assert.equal(lineFor(statement, "rentCost"), 800);

    // 50 departures x the mean of (20x5 + 50) and (30x5 + 70) = 185.
    assert.equal(lineFor(statement, "cleaningCost"), 50 * 185);

    // 40 arrivals -> 16h, 10 arrivals -> 8h, so 24h x 300.
    assert.equal(lineFor(statement, "arrivalCost"), 7200);
});

test("GOP is revenue minus every cost, and matches the rows shown", () => {
    const statement = CostData.calculateGop(data, { settingsByHotel: settings });
    const revenue = lineFor(statement, "roomRevenue") + lineFor(statement, "parkingRevenue");
    const costs = [
        "breakfastCost", "distributionCost", "franchiseCardCost", "rentCost",
        "cleaningCost", "arrivalCost"
    ].reduce((total, key) => total + lineFor(statement, key), 0);

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

    // No departure mix in this fixture, so the rate is the flat average of the
    // configured rows - which is a real limitation, and named as one.
    assert.equal(
        statement.flags.some((message) => /flat average of its configured/.test(message)),
        true,
        "the blended cleaning rate must be flagged"
    );
    assert.equal(
        statement.flags.some((message) => /only the fallback distribution %/.test(message)),
        false,
        "a property with no rulebook tree has nothing to warn about"
    );
});

// ---------------------------------------------------------------------------
// The reservation mixes
//
// Both replace an approximation the page used to apologise for. Both are weights
// only: the departure count and the room revenue they apportion still come from
// the movement and revenue facts, so a mix that is out cannot move a total.
// ---------------------------------------------------------------------------

test("cleaning is weighted by the rooms actually vacated, not by row count", () => {
    // Double at 1 guest costs 20x5 + 50 = 150; at 2 guests 30x5 + 70 = 220. The
    // flat average of the two rows is 185, but 45 of the 50 departures were
    // single-occupancy, so the real rate is much closer to 150.
    const withMix = {
        ...data,
        cleaningDepartures: [
            { stayDate: "2026-01-02", hotelName: "A", categoryName: "Double", occupancy: 1, departures: 27 },
            { stayDate: "2026-01-02", hotelName: "A", categoryName: "Double", occupancy: 2, departures: 3 },
            { stayDate: "2026-01-03", hotelName: "A", categoryName: "Double", occupancy: 1, departures: 18 },
            { stayDate: "2026-01-03", hotelName: "A", categoryName: "Double", occupancy: 2, departures: 2 }
        ]
    };
    const statement = CostData.calculateGop(withMix, { settingsByHotel: settings });

    // (45 x 150 + 5 x 220) / 50 = 157, and the 50 departures being charged for
    // are still the movement facts' own count.
    assert.equal(lineFor(statement, "cleaningCost"), 50 * 157);
    assert.equal(
        statement.flags.some((message) => /flat average of its configured/.test(message)),
        false,
        "the mix removes the caveat rather than sitting beside it"
    );

    // The API pre-aggregates this mix over the selected period. Dropping the
    // unused day dimension must produce the exact same weighted rate.
    const periodMix = {
        ...data,
        cleaningDepartures: [
            { hotelName: "A", categoryName: "Double", occupancy: 1, departures: 45 },
            { hotelName: "A", categoryName: "Double", occupancy: 2, departures: 5 }
        ]
    };
    assert.equal(
        lineFor(CostData.calculateGop(periodMix, { settingsByHotel: settings }), "cleaningCost"),
        lineFor(statement, "cleaningCost")
    );
});

test("a guest count above every configured row takes the nearest one below it", () => {
    const overflowing = {
        ...data,
        cleaningDepartures: [
            { stayDate: "2026-01-02", hotelName: "A", categoryName: "Double", occupancy: 4, departures: 30 },
            { stayDate: "2026-01-03", hotelName: "A", categoryName: "Double", occupancy: 4, departures: 20 }
        ]
    };
    const statement = CostData.calculateGop(overflowing, { settingsByHotel: settings });

    // Four guests in a category configured to two is the two-guest row, not zero.
    assert.equal(lineFor(statement, "cleaningCost"), 50 * 220);
});

test("a category with no cleaning rows is costed at the average and named", () => {
    const unknown = {
        ...data,
        cleaningDepartures: [
            { stayDate: "2026-01-02", hotelName: "A", categoryName: "Suite", occupancy: 2, departures: 30 },
            { stayDate: "2026-01-03", hotelName: "A", categoryName: "Double", occupancy: 1, departures: 20 }
        ]
    };
    const statement = CostData.calculateGop(unknown, { settingsByHotel: settings });

    // 30 at the 185 average, 20 at Double's own 150, over 50 departures.
    assert.equal(lineFor(statement, "cleaningCost"), 50 * ((30 * 185 + 20 * 150) / 50));
    assert.equal(
        statement.flags.some((message) => /Suite were costed at/.test(message)), true
    );
});

test("the mix decides the rate and never the number of departures charged", () => {
    // A mix that disagrees with the movement facts - half the departures missing -
    // must not halve the cost line. It is a weighting, not a count.
    const partial = {
        ...data,
        cleaningDepartures: [
            { stayDate: "2026-01-02", hotelName: "A", categoryName: "Double", occupancy: 1, departures: 1 }
        ]
    };
    const statement = CostData.calculateGop(partial, { settingsByHotel: settings });

    assert.equal(lineFor(statement, "cleaningCost"), 50 * 150);
});

test("one reservation cleaning is split evenly across every occupied night", () => {
    const allocated = {
        ...data,
        cleaningAllocations: [
            { stayDate: "2026-01-02", hotelName: "A", categoryName: "Double", occupancy: 1, allocatedCleanings: 1 / 3 },
            { stayDate: "2026-01-03", hotelName: "A", categoryName: "Double", occupancy: 1, allocatedCleanings: 1 / 3 },
            { stayDate: "2026-01-04", hotelName: "A", categoryName: "Double", occupancy: 1, allocatedCleanings: 1 / 3 }
        ]
    };
    const statement = CostData.calculateGop(allocated, {
        settingsByHotel: settings,
        grain: "day"
    });

    // Double/1 costs 20 x 5 + 50 = 150. The reservation still costs 150 in
    // total, but every occupied date carries exactly one third (50).
    assert.equal(lineFor(statement, "cleaningCost"), 150);
    assert.deepEqual(
        statement.periods
            .filter((period) => period.amounts.cleaningCost)
            .map((period) => [period.periodKey, period.amounts.cleaningCost]),
        [
            ["2026-01-02", 50],
            ["2026-01-03", 50],
            ["2026-01-04", 50]
        ]
    );
});

test("rent uses each revenue category on the night that earned it", () => {
    const nightly = {
        A: {
            ...settings.A,
            profile: {
                ...settings.A.profile,
                roomRentPercent: "10",
                breakfastRentPercent: "20",
                parkingRentPercent: "30"
            }
        }
    };
    const statement = CostData.calculateGop(data, {
        settingsByHotel: nightly,
        grain: "day"
    });

    assert.equal(lineFor(statement, "rentCost"),
        (10000 * 0.10 + 4000 * 0.20 + 1500 * 0.30)
        + (6000 * 0.10 + 1500 * 0.20 + 500 * 0.30));
    assert.equal(statement.periods[0].amounts.rentCost, 2250);
    assert.equal(statement.periods[1].amounts.rentCost, 1050);
});

test("SPIT can cost a lifecycle row that no longer exists in FINAL LY", () => {
    const spit = {
        roomRevenue: [{
            stayDate: "2025-10-10", hotelName: "A", amountCurrency: "SEK",
            roomRevenueExclProducts1Net: 2400,
            productRevenue1Net: 600,
            roomRevenueInclProducts1Net: 3000
        }],
        cleaningAllocations: [{
            stayDate: "2025-10-10", hotelName: "A", categoryName: "Double",
            occupancy: 1, allocatedCleanings: 1
        }]
    };
    const statement = CostData.calculateGop(spit, { settingsByHotel: settings });

    assert.equal(lineFor(statement, "roomRevenue"), 3000);
    assert.equal(lineFor(statement, "cleaningCost"), 150);
});

test("an exact lifecycle distribution mix is priced with the saved tree", () => {
    const tree = {
        A: {
            ...settings.A,
            distributionOriginGroups: [{
                groupName: "OTA", fallbackPercent: "12",
                origins: ["ChannelManager"],
                agencyGroups: [{
                    groupName: "Booking", fallbackPercent: "15",
                    filters: [{matchField: "travelAgency", containsValue: "booking"}],
                    rateGroups: [{
                        groupName: "Promo", costPercent: "20",
                        rates: [{rateName: "Summer"}]
                    }]
                }]
            }]
        }
    };
    const lifecycle = {
        ...data,
        distributionMix: [
            {stayDate: "2026-01-02", hotelName: "A", origin: "ChannelManager",
                travelAgency: "Booking.com B.V.", rateName: "Summer", roomRevenueNet: "6000"},
            {stayDate: "2026-01-02", hotelName: "A", origin: "ChannelManager",
                travelAgency: "Direct OTA", rateName: "Base", roomRevenueNet: "4000"}
        ]
    };

    // Day one: 60% at the rate override (20%) and 40% at the origin fallback
    // (12%) = 1,680. Day two has no mix and takes the property's 10% fallback.
    assert.equal(
        lineFor(CostData.calculateGop(lifecycle, {settingsByHotel: tree}), "distributionCost"),
        2280
    );
});

test("distribution charges each day at its own matched percentage", () => {
    const tree = {
        A: {
            ...settings.A,
            distributionOriginGroups: [
                { groupName: "OTA", fallbackPercent: "15", origins: ["ChannelManager"], agencyGroups: [] }
            ]
        }
    };
    const withRates = {
        ...data,
        // Day one is entirely matched at 15%; day two is half matched, so it
        // blends 15% with the property's 10% fallback.
        distributionRates: [
            { stayDate: "2026-01-02", hotelName: "A", mixRevenue: "9000", matchedRevenue: "9000", matchedPercent: "15" },
            { stayDate: "2026-01-03", hotelName: "A", mixRevenue: "8000", matchedRevenue: "4000", matchedPercent: "15" }
        ]
    };
    const statement = CostData.calculateGop(withRates, { settingsByHotel: tree });

    // 10000 at 15% plus 6000 at 12.5%.
    assert.equal(lineFor(statement, "distributionCost"), 1500 + 750);
    assert.equal(
        statement.flags.some((message) => /only the fallback distribution %/.test(message)),
        false
    );
    // Revenue no group covers is charged the fallback, and saying so is the one
    // thing that makes it fixable.
    assert.equal(
        statement.flags.some((message) => /origin no group covers/.test(message)), true
    );
});

test("a day with revenue but no imported mix falls back and says so", () => {
    const tree = {
        A: {
            ...settings.A,
            distributionOriginGroups: [
                { groupName: "OTA", fallbackPercent: "15", origins: ["ChannelManager"], agencyGroups: [] }
            ]
        }
    };
    const partial = {
        ...data,
        distributionRates: [
            { stayDate: "2026-01-02", hotelName: "A", mixRevenue: "9000", matchedRevenue: "9000", matchedPercent: "20" }
        ]
    };
    const statement = CostData.calculateGop(partial, { settingsByHotel: tree });

    // 10000 at 20%, and 2026-01-03's 6000 at the 10% fallback.
    assert.equal(lineFor(statement, "distributionCost"), 2000 + 600);
    assert.equal(
        statement.flags.some((message) => /days with no imported reservation mix/.test(message)),
        true
    );
});

test("a matched share outside nought to one cannot move the blend outside its inputs", () => {
    // A correction period can carry negative revenue on either side of the ratio.
    const fallback = CostData.effectiveDistributionPercent(null, "10");
    assert.equal(fallback, 10);
    assert.equal(
        CostData.effectiveDistributionPercent(
            { mixRevenue: "0", matchedRevenue: "0", matchedPercent: null }, "10"
        ),
        10
    );
    assert.equal(
        CostData.effectiveDistributionPercent(
            { mixRevenue: "100", matchedRevenue: "-50", matchedPercent: "30" }, "10"
        ),
        10
    );
    assert.equal(
        CostData.effectiveDistributionPercent(
            { mixRevenue: "100", matchedRevenue: "400", matchedPercent: "30" }, "10"
        ),
        30
    );
});

test("an unweighted mix is no mix at all, and reports itself as absent", () => {
    // Every row zero: there is nothing to weight by, so the caller has to fall
    // back rather than divide by zero.
    assert.equal(
        CostData.mixedCleaningCost(
            [{ categoryName: "Double", occupancy: 1, departures: 0 }],
            settings.A.cleaningCategories,
            "5"
        ),
        null
    );
    assert.equal(CostData.mixedCleaningCost([], settings.A.cleaningCategories, "5"), null);
    assert.equal(
        CostData.mixedCleaningCost(
            [{ categoryName: "Double", occupancy: 1, departures: 5 }], [], "5"
        ),
        null
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

    // Card 2% of 20000 and nothing else; rent is a separate line.
    assert.equal(lineFor(off, "franchiseCardCost"), 400);
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
    assert.equal(lineFor(inclProducts, "franchiseCardCost"), 400 + 1600);

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
    assert.equal(lineFor(exclProducts, "franchiseCardCost"), 400);
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
    assert.equal(lineFor(statement, "franchiseCardCost"), 400 + 1792);
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

// ---------------------------------------------------------------------------
// Switching a group off
// ---------------------------------------------------------------------------

test("every group counts unless the caller narrows the list", () => {
    const all = CostData.calculateGop(data, { settingsByHotel: settings });
    const listed = CostData.calculateGop(data, {
        settingsByHotel: settings, activeLines: CostData.TOGGLEABLE_KEYS
    });

    // No list at all and the full list are the same reading: the default is
    // the whole statement, not an empty one.
    assert.deepEqual(listed.lines, all.lines);
    assert.equal(listed.gop, all.gop);
    assert.equal(CostData.TOGGLEABLE_KEYS.includes("gop"), false);
});

test("a group switched off leaves the rows, GOP and every period", () => {
    const all = CostData.calculateGop(data, { settingsByHotel: settings, grain: "day" });
    const withoutCleaning = CostData.calculateGop(data, {
        settingsByHotel: settings,
        grain: "day",
        activeLines: CostData.TOGGLEABLE_KEYS.filter((key) => key !== "cleaningCost")
    });

    assert.equal(
        withoutCleaning.lines.some((line) => line.key === "cleaningCost"), false,
        "a switched-off group has no row"
    );
    // A cost that is not counted is a cost that is not subtracted, in the
    // statement and in every bar of the chart alike.
    assert.equal(withoutCleaning.gop, all.gop + lineFor(all, "cleaningCost"));
    for (const [index, period] of withoutCleaning.periods.entries()) {
        assert.equal(period.cost, all.periods[index].cost - all.periods[index].amounts.cleaningCost);
        assert.equal(period.gop, all.periods[index].gop + all.periods[index].amounts.cleaningCost);
    }
    // The blended-rate caveat explains a line that is no longer on the page.
    assert.equal(
        withoutCleaning.flags.some((message) => /flat average of its configured/.test(message)),
        false
    );
    // Warnings about the scope itself are not tied to a line and still stand.
    const noSettings = CostData.calculateGop(data, {
        settingsByHotel: {}, activeLines: ["roomRevenue"]
    });
    assert.equal(
        noSettings.flags.some((message) => /no Cost Input configuration/.test(message)), true
    );
});

test("switching a group off does not change what the other groups cost", () => {
    const all = CostData.calculateGop(data, { settingsByHotel: settings });
    const revenueOnly = CostData.calculateGop(data, {
        settingsByHotel: settings, activeLines: ["roomRevenue", "parkingRevenue"]
    });

    assert.equal(lineFor(revenueOnly, "roomRevenue"), lineFor(all, "roomRevenue"));
    assert.equal(revenueOnly.gop, 16000 + 2000);
    assert.deepEqual(
        revenueOnly.lines.map(({ key }) => key),
        ["roomRevenue", "parkingRevenue", "gop"]
    );
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

// ---------------------------------------------------------------------------
// Comparing with last year
//
// The chart draws two bars per period, so the two years have to land in the same
// buckets. They are matched on the period key rather than on bar position: a
// pairing by position slips the moment one year has a period the other does not,
// and then every bar after it compares against the wrong month.
// ---------------------------------------------------------------------------

const LosFormat = require("../frontend/los-format.js");

function factsFor(dates) {
    return {
        roomRevenue: dates.map(([stayDate, amount]) => ({
            stayDate, hotelName: "A", amountCurrency: "SEK",
            roomRevenueInclProducts1Net: String(amount)
        })),
        arrivalsDepartures: dates.map(([stayDate]) => ({
            stayDate, hotelName: "A", totalArrivals: 10, totalDepartures: 10
        }))
    };
}

test("last year's rows are restamped onto the periods they compare against", () => {
    const lastYear = factsFor([["2025-01-15", 5000], ["2025-02-15", 4000]]);
    const aligned = CostData.alignToComparison(lastYear, "sameDate", ["A"]);

    assert.deepEqual(
        aligned.roomRevenue.map((row) => row.stayDate), ["2026-01-15", "2026-02-15"]
    );
    // Every dataset is shifted, not only the one the caller happened to need:
    // the departure and revenue rows for a day have to stay on the same day, or
    // the cost and the revenue for a period stop belonging to each other.
    assert.deepEqual(
        aligned.arrivalsDepartures.map((row) => row.stayDate),
        ["2026-01-15", "2026-02-15"]
    );
    // Nothing else on the row is touched.
    assert.equal(aligned.roomRevenue[0].roomRevenueInclProducts1Net, "5000");
    assert.equal(aligned.roomRevenue[0].hotelName, "A");
});

test("both years bucket into the same period keys, on either basis", () => {
    const thisYear = factsFor([["2026-01-15", 10000], ["2026-02-15", 8000]]);
    const options = { settingsByHotel: settings, grain: "month" };

    for (const basis of ["sameDate", "sameWeekday"]) {
        const lastYear = factsFor([
            [LosFormat.lastYearDate("2026-01-15", basis), 5000],
            [LosFormat.lastYearDate("2026-02-15", basis), 4000]
        ]);
        const current = CostData.calculateGop(thisYear, options);
        const previous = CostData.calculateGop(
            CostData.alignToComparison(lastYear, basis, current.hotels), options
        );

        assert.deepEqual(
            previous.periods.map(({ periodKey }) => periodKey),
            current.periods.map(({ periodKey }) => periodKey),
            `${basis} must produce the same buckets`
        );
        // And the right figure in the right bucket, not merely the right count.
        assert.equal(previous.periods[0].revenue, 5000);
        assert.equal(previous.periods[1].revenue, 4000);
    }
});

test("a period only one year has stays unpaired instead of shifting the rest", () => {
    const thisYear = factsFor([
        ["2026-01-15", 10000], ["2026-02-15", 8000], ["2026-03-15", 6000]
    ]);
    // Nothing at all last February: the March bars must still face each other.
    const lastYear = factsFor([["2025-01-15", 5000], ["2025-03-15", 3000]]);
    const options = { settingsByHotel: settings, grain: "month" };

    const current = CostData.calculateGop(thisYear, options);
    const previous = CostData.calculateGop(
        CostData.alignToComparison(lastYear, "sameDate", current.hotels), options
    );
    const byKey = new Map(previous.periods.map((period) => [period.periodKey, period]));

    assert.deepEqual(
        current.periods.map(({ periodKey }) => periodKey),
        ["2026-01-01", "2026-02-01", "2026-03-01"]
    );
    assert.equal(byKey.get("2026-01-01").revenue, 5000);
    assert.equal(byKey.has("2026-02-01"), false, "an absent month must stay absent");
    assert.equal(byKey.get("2026-03-01").revenue, 3000);
});

test("a hotel last year no longer has in scope is left out of the comparison", () => {
    const lastYear = {
        roomRevenue: [
            { stayDate: "2025-01-15", hotelName: "A", amountCurrency: "SEK", roomRevenueInclProducts1Net: "5000" },
            { stayDate: "2025-01-15", hotelName: "Closed", amountCurrency: "SEK", roomRevenueInclProducts1Net: "9000" }
        ]
    };
    const aligned = CostData.alignToComparison(lastYear, "sameDate", ["A"]);

    // A property that has since closed would otherwise put revenue in the
    // last-year bar with nothing beside it, and read as a collapse.
    assert.deepEqual(aligned.roomRevenue.map((row) => row.hotelName), ["A"]);
    // A Set and an array are both accepted, because the caller has a Set.
    assert.equal(
        CostData.alignToComparison(lastYear, "sameDate", new Set(["A"]))
            .roomRevenue.length,
        1
    );
});

test("a row with no stay date is dropped rather than restamped to nothing", () => {
    const broken = { roomRevenue: [{ hotelName: "A", roomRevenueInclProducts1Net: "1" }, null] };
    assert.deepEqual(
        CostData.alignToComparison(broken, "sameDate", ["A"]).roomRevenue, []
    );
    assert.deepEqual(CostData.alignToComparison(null, "sameDate", ["A"]), {});
});

test("the comparison is costed under the same rulebook, so a change moves both", () => {
    // Cost Input is not versioned: the comparison answers "what would last
    // year's volumes cost to run now", which is only meaningful if both years go
    // through the same configuration.
    const lastYear = CostData.alignToComparison(
        factsFor([["2025-01-15", 5000]]), "sameDate", ["A"]
    );
    const doubled = {
        A: { ...settings.A, profile: { ...settings.A.profile, distributionDefaultPercent: "20" } }
    };

    const before = CostData.calculateGop(lastYear, { settingsByHotel: settings });
    const after = CostData.calculateGop(lastYear, { settingsByHotel: doubled });

    assert.equal(lineFor(before, "distributionCost"), 500);
    assert.equal(lineFor(after, "distributionCost"), 1000);
});
