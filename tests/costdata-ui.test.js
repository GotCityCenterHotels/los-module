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
    assert.match(/name="franchisePercent"[^>]*/.exec(html)[0], /step="0\.01"/);
    assert.match(/name="franchiseVatPercent"[^>]*/.exec(html)[0], /step="0\.01"/);
});

// The button used to sit up in the section heading, a long way above the rows
// it adds to and above the rate it applies with.
test("arrivals offers an on/off switch, then the cost, then Add threshold", () => {
    const html = read("costdata-input.html");
    const section = /data-settings-section="arrivals"[\s\S]*?<\/section>/.exec(html)[0];

    assert.match(section, /<input type="checkbox" name="arrivalCostEnabled"/);
    assert.ok(
        section.indexOf('name="receptionCostPerHour"')
            < section.indexOf('data-add="arrivalTiers"'),
        "Add threshold must come after the reception cost per hour"
    );
    assert.ok(
        section.indexOf('data-add="arrivalTiers"') < section.indexOf('id="arrivalTiers"'),
        "Add threshold must come before the threshold rows it adds to"
    );
    assert.match(section, /class="section-off-note"/);
});

test("switching a section off disables its controls but not its own switch", () => {
    const script = read("costdata-input.js");

    // The switch itself must stay live, or the section could never be turned
    // back on without a reload.
    assert.match(script, /keepEnabled\s*&&\s*keepEnabled\(control\)/);
    assert.match(script, /control === arrivalSwitch/);
    assert.match(read("styles.css"), /\.is-switched-off/);
});

test("card and franchise live in their own section, and rent no longer holds the card", () => {
    const html = read("costdata-input.html");
    const rent = /data-settings-section="rent"[\s\S]*?<\/section>/.exec(html)[0];
    const franchise = /data-settings-section="cardFranchise"[\s\S]*?<\/section>/.exec(html)[0];

    assert.doesNotMatch(rent, /cardCostPercent/);
    assert.match(franchise, /name="cardCostPercent"/);
    assert.match(franchise, /<input type="checkbox" name="franchiseEnabled"/);
    for (const field of [
        "franchisePercent", "franchiseBasis", "franchiseRevenueBase", "franchiseVatPercent"
    ]) {
        assert.match(franchise, new RegExp(`name="${field}"`), `missing ${field}`);
    }
    // Both halves of the net/gross choice, and every revenue base the
    // calculation knows how to apply.
    for (const value of [
        "net", "gross", "roomInclProducts", "roomExclProducts",
        "roomExclProductsPlusParking", "totalRevenue"
    ]) {
        assert.match(franchise, new RegExp(`value="${value}"`), `missing option ${value}`);
    }
    assert.match(html, /data-section="cardFranchise"/, "the nav must reach the new section");
});

test("the distribution editor is a three-level tree, not a flat group list", () => {
    const html = read("costdata-input.html");
    const script = read("costdata-input.js");
    const css = read("styles.css");

    assert.match(html, /id="distributionOriginGroups"/);
    assert.match(html, /data-add-origin-group/);
    assert.doesNotMatch(html, /data-add="distributionGroups"/);

    for (const builder of [
        "buildOriginGroup", "buildAgencyGroup", "buildRateGroup", "buildRatePicker"
    ]) {
        assert.match(script, new RegExp(`function ${builder}\\b`), `missing ${builder}`);
    }
    // Each level needs its own rail colour: three identical rails cannot be
    // told apart by eye.
    for (const level of [".tree-origin-group", ".tree-agency-group", ".tree-rate-group"]) {
        assert.match(css, new RegExp(level.replace(".", "\\.")), `missing ${level}`);
    }
});

test("rates can be picked from the matching list or from every rate on the property", () => {
    const script = read("costdata-input.js");

    assert.match(script, /\+ Add matching rate/);
    assert.match(script, /\+ Add any rate on the property/);
    // The full list is a separate button, so a narrowed list is never quietly
    // swapped for the unfiltered one.
    assert.match(script, /buildRatePicker\(\s*\n?\s*"matching"/);
    assert.match(script, /buildRatePicker\(\s*\n?\s*"all"/);
    assert.match(script, /rate-search/, "the picker needs its own search field");
    assert.match(script, /CostMatch\.rateAssignmentIndex/);
});

test("the agency filter searches the source without regard to case", () => {
    const script = read("costdata-input.js");

    assert.match(script, /costdata\/agencies/);
    assert.match(script, /debounce\(suggest, \d+\)/, "keystrokes must not each fire a request");
});

test("the Cost Data page offers a chart view of the same statement", () => {
    const html = read("costdata.html");

    assert.match(html, /data-gop-view="statement"/);
    assert.match(html, /data-gop-view="chart"/);
    assert.match(html, /id="gopChart"/);
    // The four marks the chart draws must each be named in the legend.
    for (const key of ["Base amount", "Profit", "Loss", "Revenue level"]) {
        assert.match(html, new RegExp(key), `missing legend entry ${key}`);
    }
});

// "−0" is a rounding artefact, not a quantity, and a column of them reads as a
// column of tiny debits.
test("a zero cost renders as plain zero, with no sign", () => {
    const script = read("costdata.js");
    const signedCost = /function signedCost\(amount\)\s*\{[\s\S]*?\n {4}\}/.exec(script)[0];

    assert.match(signedCost, /if \(rounded === 0\) return LosFormat\.formatSek\(0\)/);
    assert.ok(
        signedCost.indexOf("rounded === 0") < signedCost.indexOf("rounded < 0"),
        "the zero case must be settled before either sign is applied"
    );
    // The integer columns go through Intl, which formats -0 as "-0".
    assert.match(script, /integerFormatter\.format\(Number\(value\) \+ 0\)/);
});

// ---------------------------------------------------------------------------
// Regressions found by review. Each of these shipped broken once.
// ---------------------------------------------------------------------------

test("a combo closes the previously open popup before unhiding its own", () => {
    const script = read("costdata-input.js");

    // Transposed, the second keystroke found this combo registered as the open
    // one and closed it again, so refining an agency search hid the list for
    // good. Every combo in the file must close-then-open, never the reverse.
    for (const block of script.split("closeOpenCombo();").slice(1)) {
        assert.doesNotMatch(
            block.slice(0, 80),
            /^\s*openCombo = api;\s*\n\s*popup\.hidden = false/,
            "closeOpenCombo() must not be followed by unhiding the same popup"
        );
    }
    assert.match(script, /closeOpenCombo\(\);\s*\n\s*popup\.hidden = false;\s*\n\s*openCombo = api;/);
});

test("a rate picked from the source keeps its Mews id", () => {
    const script = read("costdata-input.js");

    // partitionOptions rebuilds its results as {name} and drops everything
    // else, so reading item.id straight off the option stored null every time.
    assert.match(script, /function sourceRateId\(name\)/);
    assert.match(script, /rateId: sourceRateId\(item\.name\)/);
    assert.doesNotMatch(script, /rateId: item\.id/);
});

test("caches that hold per-property source data are keyed and cleared by property", () => {
    const script = read("costdata-input.js");

    assert.match(script, /rateCache\.clear\(\);/);
    assert.match(script, /agencyCache\.clear\(\);/);
    // Without the enterprise id in the key, searching the same term on two
    // properties served the first property's agencies for the second.
    assert.match(script, /const key = `\$\{loadedEnterpriseId\}\|/);
});

test("an off section's blank required field does not travel to the server", () => {
    const script = read("costdata-input.js");

    // Disabling exempts a field from constraint validation, so a blank one
    // sailed past reportValidity and then failed the whole PUT with a 400
    // naming a field the page had greyed out.
    assert.match(script, /if \(input\.disabled && input\.value === ""\) continue;/);
});

test("collect builds a pruned copy instead of editing the model in place", () => {
    const script = read("costdata-input.js");
    const collect = /function collect\(\)[\s\S]*?\n {4}\}/.exec(script)[0];

    // Splicing the model's own arrays left the still-mounted rows holding
    // indices into an array that had shrunk under them after a rejected save.
    assert.doesNotMatch(collect, /agency\.filters = /);
    assert.doesNotMatch(collect, /group\.rules = /);
    assert.match(collect, /return \{\s*\n\s*\.\.\.model,/);
});

test("text typed into the origin box survives an edit elsewhere in the tree", () => {
    const script = read("costdata-input.js");

    // Every tree edit rebuilds the whole tree, so anything not in the model is
    // destroyed unless it is parked somewhere keyed by the group.
    assert.match(script, /originDrafts = new WeakMap\(\)/);
    assert.match(script, /entry\.value = originDrafts\.get\(group\) \|\| ""/);
});

test("the chart axis spans both sides of zero so a reversal is drawn to scale", () => {
    const script = read("costdata.js");

    // Clamping the scale at zero drew a half-million krona correction as a few
    // pixels of red under a tooltip reporting the real figure.
    assert.match(script, /const minValue = -niceCeiling\(-Math\.min\(0, \.\.\.values\)\)/);
    assert.match(script, /const verticalBand = \(from, to\) =>/);
    assert.doesNotMatch(script, /const revenue = Math\.max\(0, period\.revenue\)/);
    assert.doesNotMatch(script, /const cost = Math\.max\(0, period\.cost\)/);
    assert.match(script, /gop-bar-zero/, "a signed axis needs a visible zero line");
});

test("the picker payload scans the reservation table once, not twice", () => {
    const service = fs.readFileSync(
        path.join(__dirname, "..", "services", "cost_source_service.py"), "utf8"
    );
    const fetch = /def _fetch_cost_sources_uncached[\s\S]*?\n    \}/.exec(service)[0];

    // Channels and origins are the same column under two names, so calling
    // both ran the page's most expensive aggregate twice per cold load.
    assert.doesNotMatch(fetch, /list_channels\(/);
    assert.match(fetch, /"channels": \[\s*\n\s*\{"id": origin\["name"\]/);
});

// ---------------------------------------------------------------------------
// Cleaning: bed types, inheritance, and a much denser layout
// ---------------------------------------------------------------------------

test("bed types sit between the cost per minute and the room categories", () => {
    const html = read("costdata-input.html");
    const section = /data-settings-section="cleaning"[\s\S]*?<\/section>/.exec(html)[0];

    assert.match(section, /id="bedTypes"/);
    assert.match(section, /data-add-bed-type/);
    assert.ok(
        section.indexOf('name="cleaningCostPerMinute"') < section.indexOf('id="bedTypes"'),
        "bed types belong below the cleaning cost per minute"
    );
    assert.ok(
        section.indexOf('id="bedTypes"') < section.indexOf('id="cleaningCategories"'),
        "bed types belong above the room categories that use them"
    );
});

test("rooms are bound to a bed type by a stable key, never by its name", () => {
    const script = read("costdata-input.js");

    // The name is what gets persisted, but it is also what the operator is
    // editing one keystroke at a time. Matching rooms on it meant that typing
    // "Sofa bed" beside an existing "Sofa" re-pointed Sofa's rooms on the way
    // past, that clearing the field to retype it unbound every room for good,
    // and that deleting one of two identically named beds stripped the
    // other's rooms.
    assert.match(script, /function ensureBedKeys\(\)/);
    assert.match(script, /function bedByKey\(key\)/);
    assert.match(script, /function resolveRowBed\(rowBed\)/);
    assert.doesNotMatch(script, /renameBedEverywhere/);
    assert.match(script, /function removeBedEverywhere\(bedKey\)/);
    assert.match(script, /bed\.bedKey !== bedKey/);
    // Keys never leave the editor; the save writes the current names back out.
    assert.match(script, /if \(resolved\.bed\) bed\.bedName = resolved\.bed\.bedName;/);
});

test("a stale occupancy inside a live category is still marked and removable", () => {
    const script = read("costdata-input.js");

    // Mews dropping an extra bed shrinks a category from 1-3 to 1-2, stranding
    // the saved occupancy-3 row inside a category that still exists. Gating on
    // the category left it indistinguishable from a real row, undeletable, and
    // still skewing the blended per-departure cost.
    assert.match(script, /if \(!row\.fromHotel\) \{/);
    assert.match(script, /line\.classList\.add\("is-orphaned-row"\)/);
    assert.match(read("styles.css"), /\.is-orphaned-row/);
});

test("an emptied bed count settles at one instead of failing the save", () => {
    const script = read("costdata-input.js");
    const service = fs.readFileSync(
        path.join(__dirname, "..", "services", "cost_settings_service.py"), "utf8"
    );

    // A cleared number box sends "", which passed the browser's check and then
    // failed the whole PUT with a message naming the bed rather than the box.
    assert.match(script, /quantity\.required = true;/);
    assert.match(script, /quantity\.value = "1";/);
    assert.match(service, /1 if raw_quantity in \(None, ""\) else raw_quantity/);
});

test("the lowest guest count carries the setup and the rest inherit it", () => {
    const script = read("costdata-input.js");

    assert.match(script, /function baseRowFor\(categoryName\)/);
    assert.match(script, /function effectiveRow\(row\)/);
    // Beds inherit unless the row's override is switched on; minutes inherit
    // whenever the box is empty. The two rules are deliberately different.
    assert.match(script, /const inheritsBeds = !isBase && !row\.overridesBase/);
    assert.match(
        script,
        /const inheritsMinutes = !isBase && \(rawMinutes === null \|\| rawMinutes === undefined \|\| rawMinutes === ""\)/
    );
    // Switching the override on must start from what was being inherited, not
    // from an empty row.
    assert.match(script, /row\.beds = state\.beds\.map\(bed => \(\{\.\.\.bed\}\)\)/);
});

test("an inherited minute count shows greyed in the box rather than as a value", () => {
    const script = read("costdata-input.js");
    const css = read("styles.css");

    assert.match(script, /minutes\.placeholder = state\.minutes/);
    // A placeholder, not a value: typing in the other counts stays optional
    // and the field still reads as empty when it is.
    assert.match(script, /row\.cleaningMinutes = minutes\.value === "" \? null : minutes\.value/);
    assert.match(css, /\.cleaning-minutes::placeholder/);
});

test("categories with nothing chosen are called out, and counted", () => {
    const html = read("costdata-input.html");
    const script = read("costdata-input.js");

    assert.match(html, /id="cleaningProgress"/);
    assert.match(script, /function categoryIsSet\(group\)/);
    assert.match(script, /categories have beds set/);
    assert.match(script, /No beds set/);
});

test("the room category layout is a dense grid, not a bordered row each", () => {
    const script = read("costdata-input.js");
    const css = read("styles.css");

    // One block per category with one line per occupancy; the old layout spent
    // a 60px bordered .rule-row plus an h3 on every single occupancy.
    assert.match(script, /className = "cleaning-grid-row"/);
    assert.doesNotMatch(script, /className = "rule-row cleaning-row"/);
    assert.match(css, /\.cleaning-grid-row \{ display:grid;/);
    // Labels appear once per category, in the header, not on every field.
    assert.match(script, /className = "cleaning-grid-head"/);
});

test("the cost calculation reads the resolved figures, not the raw ones", () => {
    const CostData = require("../frontend/costdata-data.js");

    // An inheriting row has no figures of its own; reading its raw fields
    // would cost it at zero minutes and zero linen.
    const settings = {
        A: {
            profile: { currency: "SEK", cleaningCostPerMinute: "5" },
            cleaningCategories: [
                { categoryName: "D", occupancy: 1, cleaningMinutes: "30", linenCost: "0",
                  effectiveCleaningMinutes: "30", effectiveLinenCost: "75" },
                { categoryName: "D", occupancy: 2, cleaningMinutes: null, linenCost: "0",
                  effectiveCleaningMinutes: "30", effectiveLinenCost: "75" }
            ],
            arrivalTiers: [], breakfastTiers: [], distributionGroups: []
        }
    };
    const data = {
        arrivalsDepartures: [
            { stayDate: "2026-01-02", hotelName: "A", totalArrivals: 0, totalDepartures: 10 }
        ]
    };
    const statement = CostData.calculateGop(data, { settingsByHotel: settings });

    // Both rows resolve to 30 min x 5 kr + 75 kr = 225, so 10 departures cost
    // 2250. Reading the raw fields would have given half that.
    assert.equal(
        statement.lines.find((line) => line.key === "cleaningCost").amount, 2250
    );
});
