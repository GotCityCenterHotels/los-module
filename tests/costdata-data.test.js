const test = require("node:test");
const assert = require("node:assert/strict");

const CostData = require("../frontend/costdata-data.js");

const data = {
    roomRevenue: [
        {
            stayDate: "2026-01-02",
            hotelName: "A",
            amountCurrency: "SEK",
            roomRevenueExclProducts1Net: "100.25",
            productRevenue1Net: "25.50",
            roomRevenueInclProducts1Net: "125.75",
            lastUpdatedAt: "2026-01-03T08:00:00+00:00"
        },
        {
            stayDate: "2026-01-20",
            hotelName: "A",
            amountCurrency: "SEK",
            roomRevenueExclProducts1Net: "200.00",
            productRevenue1Net: "40.00",
            roomRevenueInclProducts1Net: "240.00",
            lastUpdatedAt: "2026-01-21T08:00:00+00:00"
        },
        {
            stayDate: "2026-01-20",
            hotelName: "B",
            amountCurrency: "EUR",
            roomRevenueExclProducts1Net: "50.00",
            productRevenue1Net: "5.00",
            roomRevenueInclProducts1Net: "55.00"
        }
    ],
    payments: [
        { stayDate: "2026-01-02", hotelName: "A", amountCurrency: "SEK", totalPaymentAmountGrossValue: "150" }
    ],
    breakfast: [
        { stayDate: "2026-01-02", hotelName: "A", breakfastTotal: 4, breakfastNetCost: "80.25" }
    ],
    parking: [
        { stayDate: "2026-01-02", hotelName: "A", service: "Garage", totalReservationsUsingParking: 2, totalParkingSpots: 3, totalParkingAmountNetValue: "90" }
    ],
    arrivalsDepartures: [
        { stayDate: "2026-01-02", hotelName: "A", totalArrivals: 8, totalDepartures: 6 }
    ]
};

test("monthly revenue aggregation preserves hotel and currency grain", () => {
    const rows = CostData.aggregate("roomRevenue", data.roomRevenue, { grain: "month" });

    assert.equal(rows.length, 2);
    assert.equal(rows[0].stayDate, "2026-01-01");
    assert.equal(rows[0].hotelName, "A");
    assert.equal(rows[0].amountCurrency, "SEK");
    assert.equal(rows[0].roomRevenueInclProducts1Net, 365.75);
    assert.equal(rows[0].lastUpdatedAt, "2026-01-21T08:00:00+00:00");
});

test("hotel filtering is applied before aggregation", () => {
    const rows = CostData.aggregate("roomRevenue", data.roomRevenue, {
        grain: "year",
        hotelName: "B"
    });

    assert.equal(rows.length, 1);
    assert.equal(rows[0].hotelName, "B");
    assert.equal(rows[0].roomRevenueInclProducts1Net, 55);
});

test("summaries keep currencies separate and total operational values", () => {
    const summary = CostData.summarize(data);

    assert.deepEqual(summary.roomRevenue, { SEK: 365.75, EUR: 55 });
    assert.deepEqual(summary.payments, { SEK: 150 });
    assert.equal(summary.breakfastCost, 80.25);
    assert.equal(summary.parkingNet, 90);
    assert.equal(summary.arrivals, 8);
    assert.equal(summary.departures, 6);
});

test("unknown datasets fail explicitly", () => {
    assert.throws(() => CostData.aggregate("unknown", []), /Unknown cost dataset/);
});

test("every grain buckets to the first date of its period", () => {
    // Same keying as los-data.js, so a period label means the same thing on
    // every page: weeks start on Monday.
    assert.equal(CostData.periodKey("2026-01-08", "day"), "2026-01-08");
    assert.equal(CostData.periodKey("2026-01-08", "week"), "2026-01-05");
    assert.equal(CostData.periodKey("2026-01-05", "week"), "2026-01-05");
    assert.equal(CostData.periodKey("2026-01-04", "week"), "2025-12-29");
    assert.equal(CostData.periodKey("2026-01-08", "month"), "2026-01-01");
    assert.equal(CostData.periodKey("2026-01-08", "year"), "2026-01-01");
});

test("weekly aggregation collapses a Monday-to-Sunday span into one row", () => {
    // 2 Jan (Friday) and 4 Jan (Sunday) share the week beginning 29 Dec;
    // 20 Jan starts its own.
    const rows = CostData.aggregate("roomRevenue", [
        ...data.roomRevenue,
        {
            stayDate: "2026-01-04",
            hotelName: "A",
            amountCurrency: "SEK",
            roomRevenueInclProducts1Net: "10.00"
        }
    ], { grain: "week", hotelName: "A" });

    assert.equal(rows.length, 2);
    assert.equal(rows[0].stayDate, "2025-12-29");
    assert.equal(rows[0].roomRevenueInclProducts1Net, 135.75);
    assert.equal(rows[1].stayDate, "2026-01-19");
});
