const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const frontend = path.join(__dirname, "..", "frontend");
const read = (file) => fs.readFileSync(path.join(frontend, file), "utf8");

test("the Cost Data dashboard is the GOP statement, with no gross figures", () => {
    const html = read("costdata.html");

    assert.match(html, /id="gopStatement"/);
    assert.match(html, /id="gopRows"/);
    assert.match(html, /id="gopFlags"/);
    assert.match(html, /NET · EXCL\. VAT/);
    // The old summary strip led with a VAT-inclusive payments total.
    assert.doesNotMatch(html, /id="costSummary"/);
    assert.doesNotMatch(html, /Total payments gross/);
});

test("every grain the period column supports is offered on Cost Data", () => {
    const html = read("costdata.html");
    for (const grain of ["day", "week", "month", "year"]) {
        assert.match(html, new RegExp(`<option value="${grain}"`), `missing ${grain}`);
    }
});

test("the shared formatter loads before anything that formats with it", () => {
    const pages = {
        "index.html": "app.js",
        "distribution.html": "distribution.js",
        "costdata.html": "costdata.js",
        "costdata-input.html": "costdata-input.js"
    };
    for (const [page, consumer] of Object.entries(pages)) {
        const html = read(page);
        const formatAt = html.indexOf("los-format.js");
        const consumerAt = html.indexOf(consumer);
        assert.notEqual(formatAt, -1, `${page} should load los-format.js`);
        assert.ok(formatAt < consumerAt, `${page} loads ${consumer} before los-format.js`);
    }
});

test("breakfast thresholds read from, to, staff hours on one compact row", () => {
    const script = read("costdata-input.js");
    const breakfast = /breakfastTiers:\s*\[(.*?)\]\s*\n/s.exec(script)[1];

    assert.match(breakfast, /"minGuests","From guests"/);
    assert.match(breakfast, /"maxGuests","To guests"/);
    assert.match(breakfast, /"staffHours","Staff hours"/);
    assert.ok(
        breakfast.indexOf("minGuests") < breakfast.indexOf("maxGuests")
            && breakfast.indexOf("maxGuests") < breakfast.indexOf("staffHours"),
        "fields must be ordered from, to, hours"
    );
    assert.match(script, /rowClasses\s*=\s*\{\s*breakfastTiers:\s*"tier-row is-compact"/);
    assert.match(read("costdata-input.html"), /id="breakfastTiers" class="rule-list is-compact-list"/);
    assert.match(read("styles.css"), /\.tier-row\.is-compact/);
});

test("SEK inputs step in whole kronor, not ore", () => {
    const html = read("costdata-input.html");
    for (const field of [
        "cleaningCostPerMinute",
        "receptionCostPerHour",
        "breakfastFoodCostPerGuest",
        "breakfastStaffCostPerHour"
    ]) {
        const input = new RegExp(`name="${field}"[^>]*`).exec(html)[0];
        assert.match(input, /step="1"/, `${field} should step in whole kronor`);
    }
    // Percentages are not money and keep their decimals.
    assert.match(/name="cardCostPercent"[^>]*/.exec(html)[0], /step="0\.01"/);
});
