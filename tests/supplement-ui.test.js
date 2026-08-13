const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const frontend = path.join(__dirname, "..", "frontend");

test("every public page links to Supplement", () => {
    const pages = ["index.html", "distribution.html", "costdata.html", "costdata-input.html", "supplement.html"];
    for (const page of pages) {
        const html = fs.readFileSync(path.join(frontend, page), "utf8");
        assert.match(html, /href="\/supplement\.html"/, `${page} should link to Supplement`);
    }
});

test("Supplement page exposes its controls, table landmark, and accessible dialog", () => {
    const html = fs.readFileSync(path.join(frontend, "supplement.html"), "utf8");
    const requiredIds = [
        "supplementHotel",
        "categoryOptions",
        "hotelVisibilityOptions",
        "supplementStartDate",
        "supplementEndDate",
        "supplementLyBasis",
        "supplementInventoryBasis",
        "supplementDiffMode",
        "supplementTableRegion",
        "supplementDetailDialog"
    ];
    for (const id of requiredIds) assert.match(html, new RegExp(`id="${id}"`));
    assert.match(html, /aria-current="page">Supplement/);
    assert.match(html, /Loading Supplement data/);
    assert.doesNotMatch(html, /Simulated preview data/);
    assert.match(html, /<dialog[^>]+aria-labelledby="detailTitle"/);
    assert.match(html, /id="dateWindowNav"/);
});
