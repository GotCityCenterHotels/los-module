const test = require("node:test");
const assert = require("node:assert/strict");

const LosApi = require("../frontend/los-api.js");

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
