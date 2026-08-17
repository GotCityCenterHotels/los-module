const test = require("node:test");
const assert = require("node:assert/strict");

const LosApi = require("../frontend/los-api.js");

function createStorage() {
    const values = new Map();
    return {
        getItem: (key) => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value)
    };
}

test("non-JSON API errors preserve HTTP status and response text", async () => {
    const originalFetch = global.fetch;
    global.fetch = async () => ({
        ok: false,
        status: 503,
        text: async () => "Function host is not running."
    });

    try {
        await assert.rejects(
            LosApi.fetchJson("/api/los/facts"),
            /API returned HTTP 503: Function host is not running\./
        );
    }
    finally {
        global.fetch = originalFetch;
    }
});

test("JSON API errors use the safe server error message", async () => {
    const originalFetch = global.fetch;
    global.fetch = async () => ({
        ok: false,
        status: 400,
        text: async () => JSON.stringify({ error: "Invalid lyComparisonBasis" })
    });

    try {
        await assert.rejects(
            LosApi.fetchJson("/api/los/facts"),
            /Invalid lyComparisonBasis/
        );
    }
    finally {
        global.fetch = originalFetch;
    }
});

test("selected months become sorted contiguous request ranges", () => {
    assert.deepEqual(
        LosApi.buildContiguousMonthRanges(
            ["2027-01", "2026-12", "2026-10", "2026-11", "2027-03"],
            "2026-10-01",
            "2027-03-31"
        ),
        [
            { startDate: "2026-10-01", endDate: "2027-01-31" },
            { startDate: "2027-03-01", endDate: "2027-03-31" }
        ]
    );
    assert.deepEqual(
        LosApi.buildContiguousMonthRanges([], "2026-02-03", "2026-02-17"),
        [{ startDate: "2026-02-03", endDate: "2026-02-17" }]
    );
});

test("fact collections merge duplicate additive keys", () => {
    const fact = {
        arrivalDate: "2026-01-01",
        hotelCode: "A",
        scenario: "current",
        los: 2,
        bookingCount: 3,
        nightCount: 6
    };
    const merged = LosApi.mergeFactRows([[fact], [{ ...fact, bookingCount: 2, nightCount: 4 }]]);

    assert.equal(merged.length, 1);
    assert.equal(merged[0].bookingCount, 5);
    assert.equal(merged[0].nightCount, 10);
});

test("non-contiguous fact ranges overlap, up to the concurrency cap", async () => {
    const requestedUrls = [];
    let active = 0;
    let maxActive = 0;
    const fetcher = async (url) => {
        requestedUrls.push(url);
        active += 1;
        maxActive = Math.max(maxActive, active);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active -= 1;
        return { data: [] };
    };

    await LosApi.fetchLosFactRanges({
        startDate: "2026-01-01",
        endDate: "2026-03-31",
        lyComparisonBasis: "sameDate",
        selectedMonths: ["2026-01", "2026-03"],
        fetcher
    });

    assert.equal(requestedUrls.length, 2);
    assert.equal(maxActive, 2);
    assert.match(requestedUrls[0], /startDate=2026-01-01/);
    assert.match(requestedUrls[1], /startDate=2026-03-01/);
});

test("more ranges than the concurrency cap queue instead of all firing", async () => {
    let active = 0;
    let maxActive = 0;
    const fetcher = async () => {
        active += 1;
        maxActive = Math.max(maxActive, active);
        await new Promise((resolve) => setTimeout(resolve, 2));
        active -= 1;
        return { data: [] };
    };

    // Six single-month selections, each of which becomes its own range.
    const selectedMonths = ["2026-01", "2026-03", "2026-05", "2026-07", "2026-09", "2026-11"];
    await LosApi.fetchLosFactRanges({
        startDate: "2026-01-01",
        endDate: "2026-11-30",
        lyComparisonBasis: "sameDate",
        selectedMonths,
        fetcher
    });

    assert.equal(maxActive, LosApi.MAX_CONCURRENT_RANGE_REQUESTS);
    assert.ok(maxActive < selectedMonths.length);
});

test("disjoint ranges concatenate every row without re-keying", async () => {
    const fetcher = async (url) => ({
        data: [{
            arrivalDate: url.includes("2026-01") ? "2026-01-04" : "2026-03-04",
            hotelCode: "A",
            scenario: "current",
            los: 2,
            bookingCount: 3,
            nightCount: 6
        }]
    });

    const result = await LosApi.fetchLosFactRanges({
        startDate: "2026-01-01",
        endDate: "2026-03-31",
        lyComparisonBasis: "sameDate",
        selectedMonths: ["2026-01", "2026-03"],
        fetcher
    });

    assert.equal(result.rowCount, 2);
    assert.deepEqual(
        result.data.map(({ arrivalDate }) => arrivalDate).sort(),
        ["2026-01-04", "2026-03-04"]
    );
});

test("healthy JSON responses are parsed without materialising the body as text", async () => {
    const originalFetch = global.fetch;
    let textCalls = 0;
    global.fetch = async () => ({
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({ data: [{ los: 1 }] }),
        text: async () => { textCalls += 1; return "{}"; }
    });

    try {
        const payload = await LosApi.fetchJson("/api/los/facts");
        assert.deepEqual(payload, { data: [{ los: 1 }] });
        assert.equal(textCalls, 0);
    }
    finally {
        global.fetch = originalFetch;
    }
});

test("range failures identify the range and return no partial result", async () => {
    let calls = 0;
    const fetcher = async () => {
        calls += 1;
        if (calls === 2) throw new Error("gateway unavailable");
        return { data: [{ arrivalDate: "2026-01-01" }] };
    };

    await assert.rejects(
        LosApi.fetchLosFactRanges({
            startDate: "2026-01-01",
            endDate: "2026-03-31",
            lyComparisonBasis: "sameDate",
            selectedMonths: ["2026-01", "2026-03"],
            fetcher
        }),
        /2026-03-01 to 2026-03-31: gateway unavailable/
    );
    assert.equal(calls, 2);
});

test("hotel metadata uses session cache and coalesces in-flight requests", async () => {
    const storage = createStorage();
    let calls = 0;
    let releaseRequest;
    const fetcher = () => {
        calls += 1;
        return new Promise((resolve) => {
            releaseRequest = () => resolve({ data: ["A", "B"] });
        });
    };
    const request = {
        apiBaseUrl: "/test-api",
        startDate: "2026-01-01",
        endDate: "2026-12-31",
        lyComparisonBasis: "sameDate",
        storage,
        fetcher
    };

    const first = LosApi.fetchHotelList(request);
    const second = LosApi.fetchHotelList(request);
    assert.equal(calls, 1);
    releaseRequest();
    assert.deepEqual((await first).data, ["A", "B"]);
    assert.deepEqual((await second).data, ["A", "B"]);

    const cached = await LosApi.fetchHotelList({ ...request, now: Date.now() + 1000 });
    assert.equal(cached.fromCache, true);
    assert.equal(calls, 1);
});
