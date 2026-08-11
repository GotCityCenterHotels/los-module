const test = require("node:test");
const assert = require("node:assert/strict");

const LosData = require("../frontend/los-data.js");

const facts = [
    { arrivalDate: "2026-01-01", hotelCode: "A", scenario: "current", los: 1, bookingCount: 2, nightCount: 2 },
    { arrivalDate: "2026-01-02", hotelCode: "A", scenario: "current", los: 5, bookingCount: 1, nightCount: 5 },
    { arrivalDate: "2026-01-02", hotelCode: "B", scenario: "current", los: 3, bookingCount: 4, nightCount: 12 },
    { arrivalDate: "2026-02-01", hotelCode: "A", scenario: "ly", los: 2, bookingCount: 2, nightCount: 4 },
    { arrivalDate: "2026-02-01", hotelCode: "A", scenario: "spit", los: 8, bookingCount: 1, nightCount: 8 }
];

test("weighted Average LOS uses additive nights and bookings", () => {
    const [row] = LosData.calculateAverageLos(facts, {
        grain: "month",
        hotelCodes: ["A", "B"],
        scenario: "current",
        portfolio: true
    });

    assert.equal(row.bookingCount, 7);
    assert.equal(row.nightCount, 19);
    assert.equal(row.averageLos, 19 / 7);
    assert.notEqual(row.averageLos, ((7 / 3) + 3) / 2);
});

test("portfolio aggregation sums hotels", () => {
    const rows = LosData.aggregateFacts(facts, {
        grain: "month",
        scenario: "current",
        portfolio: true
    });

    assert.equal(rows.reduce((sum, row) => sum + row.bookingCount, 0), 7);
    assert.ok(rows.every((row) => row.hotelCode === "Total"));
});

test("daily facts aggregate into month and year keys", () => {
    assert.equal(LosData.getPeriodKey("2026-07-18", "day"), "2026-07-18");
    assert.equal(LosData.getPeriodKey("2026-07-18", "month"), "2026-07-01");
    assert.equal(LosData.getPeriodKey("2026-07-18", "year"), "2026-01-01");
    assert.equal(LosData.getPeriodKey("2026-07-19", "week"), "2026-07-13");
});

test("Current, LY, and SPIT remain separate", () => {
    const rows = LosData.calculateAverageLos(facts, { grain: "year", portfolio: true });
    assert.deepEqual(rows.map((row) => row.scenario), ["current", "ly", "spit"]);
    assert.deepEqual(rows.map((row) => row.bookingCount), [7, 2, 1]);
});

test("default buckets keep exact LOS 5 and 8 in 5+", () => {
    assert.equal(LosData.getLosBucket(5), "5+");
    assert.equal(LosData.getLosBucket(8), "5+");
});

test("alternative bucket definitions require no fact changes", () => {
    const alternative = [
        { label: "1", min: 1, max: 1 },
        { label: "2", min: 2, max: 2 },
        { label: "3-4", min: 3, max: 4 },
        { label: "5-7", min: 5, max: 7 },
        { label: "8+", min: 8, max: Infinity }
    ];

    assert.equal(LosData.getLosBucket(3, alternative), "3-4");
    assert.equal(LosData.getLosBucket(6, alternative), "5-7");
    assert.equal(LosData.getLosBucket(8, alternative), "8+");
});

test("booking distribution percentages use booking counts", () => {
    const [row] = LosData.calculateDistribution(facts, {
        grain: "month",
        scenario: "current",
        portfolio: true,
        metric: "bookings"
    });

    assert.equal(row.total, 7);
    assert.deepEqual(row.values.map(({ value }) => value), [2, 0, 4, 0, 1]);
    assert.ok(Math.abs(
        row.values.reduce((sum, item) => sum + item.percentage, 0) - 100
    ) < 1e-10);
});

test("room-night distribution percentages use night counts", () => {
    const [row] = LosData.calculateDistribution(facts, {
        grain: "month",
        scenario: "current",
        portfolio: true,
        metric: "nights"
    });

    assert.equal(row.total, 19);
    assert.deepEqual(row.values.map(({ value }) => value), [2, 0, 12, 0, 5]);
    assert.ok(Math.abs(
        row.values.reduce((sum, item) => sum + item.percentage, 0) - 100
    ) < 1e-10);
});

test("empty datasets return empty analytical views", () => {
    assert.deepEqual(LosData.calculateAverageLos([]), []);
    assert.deepEqual(LosData.calculateDistribution([]), []);
});

test("zero booking counts yield a null average and zero percentages", () => {
    const zeroFacts = [
        { arrivalDate: "2026-01-01", hotelCode: "A", scenario: "current", los: 1, bookingCount: 0, nightCount: 0 }
    ];
    const [average] = LosData.calculateAverageLos(zeroFacts);
    const [distribution] = LosData.calculateDistribution(zeroFacts);

    assert.equal(average.averageLos, null);
    assert.deepEqual(distribution.values.map(({ percentage }) => percentage), [0, 0, 0, 0, 0]);
});
