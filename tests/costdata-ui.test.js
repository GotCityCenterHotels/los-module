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

// The switch used to sit in the section heading, at the far right of the page
// and a long way above the fields it governs, so nothing on screen connected
// the two.
test("the franchise switch sits in one card with the fields it governs", () => {
    const html = read("costdata-input.html");
    const section = /data-settings-section="cardFranchise"[\s\S]*?<\/section>/.exec(html)[0];
    const heading = /<div class="editor-heading">[\s\S]*?<\/div><\/div>/.exec(section)[0];
    const card = /<div class="franchise-card"[\s\S]*?\n {16}<\/div>/.exec(section)[0];

    assert.doesNotMatch(heading, /franchiseEnabled/, "the switch has left the heading");
    assert.match(card, /<input type="checkbox" name="franchiseEnabled"/);
    // One card, holding every franchise field - not a card per field.
    for (const field of [
        "franchisePercent", "franchiseBasis", "franchiseRevenueBase", "franchiseVatPercent"
    ]) {
        assert.match(card, new RegExp(`name="${field}"`), `${field} belongs in the card`);
    }
    assert.match(read("styles.css"), /\.franchise-card \{/);
});

test("the VAT field only appears when the franchise is calculated on gross", () => {
    const html = read("costdata-input.html");
    const script = read("costdata-input.js");

    // Hidden to start with: net is the default basis, and a field that does
    // nothing is worse than no field at all.
    assert.match(html, /<label id="franchiseVatField" hidden>/);
    assert.match(script, /const grossBasis = Boolean\(basis\) && basis\.value === "gross"/);
    assert.match(script, /if \(vatField\) vatField\.hidden = !grossBasis;/);
    // Hidden and disabled together: disabling is what exempts a blank required
    // field from constraint validation, so hiding alone would block the save.
    assert.match(script, /if \(vat\) vat\.disabled = !franchiseOn \|\| !grossBasis;/);
    assert.match(script, /event\.target\.name === "franchiseBasis"/);
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

test("the Cost Data page charts the same statement it tabulates", () => {
    const html = read("costdata.html");

    assert.match(html, /id="gopChartPanel"/);
    assert.match(html, /id="gopChart"/);
    // Every mark the chart draws is named in the legend, or a reader has no
    // way to tell the base from the profit sitting on top of it.
    for (const key of ["Base amount", "Profit", "Loss", "Revenue level"]) {
        assert.match(html, new RegExp(key), `missing legend entry ${key}`);
    }
    // The statement and the chart show the same set of lines, so the control
    // that narrows them has to drive both.
    assert.match(html, /id="gopLineToggles"/);
});

// The chart used to be the other half of a Statement/Chart switcher, so it was
// only ever seen by someone who knew to go looking for it.
test("query settings sit beside the statement, and the chart under both", () => {
    const html = read("costdata.html");
    const overview = /<div class="cost-overview">[\s\S]*?\n {8}<\/div>/.exec(html)[0];

    // Settings on the left, statement on the right, inside one row.
    assert.ok(
        overview.indexOf('class="filter-panel cost-filters cost-query"')
            < overview.indexOf('id="gopStatement"'),
        "the query settings must come before the statement in the overview row"
    );
    // And the chart below that row, not inside it.
    assert.doesNotMatch(overview, /id="gopChartPanel"/);
    assert.ok(
        html.indexOf('class="cost-overview"') < html.indexOf('id="gopChartPanel"'),
        "the chart panel belongs under the settings and the statement"
    );
    // Nothing hides the chart behind a view any more.
    assert.doesNotMatch(html, /data-gop-view/);
    assert.doesNotMatch(read("costdata.js"), /setGopView/);
    assert.match(read("styles.css"), /\.cost-overview \{/);
});

test("the revenue marker is exactly as wide as the bar it marks", () => {
    const script = read("costdata.js");
    const css = read("styles.css");

    // It used to overhang by 5px each side, which at a daily grain - where the
    // bars are only a few pixels apart - drew it across its neighbours.
    assert.match(script, /const marker = barWidth \/ 2;/);
    assert.doesNotMatch(script, /barWidth \/ 2 \+ \d/);
    // A round cap puts half the stroke width back on each end, so the CSS has
    // to agree with the geometry.
    const revenueRule = /\.gop-bar-revenue \{[\s\S]*?\}/.exec(css)[0];
    assert.match(revenueRule, /stroke-linecap: butt/);
});

test("every group starts shown and can be switched off from the settings", () => {
    const script = read("costdata.js");
    const html = read("costdata.html");

    // Built from the statement's own line list, so a line added to the
    // calculation cannot be missing a switch.
    assert.match(script, /CostData\.GOP_LINES\s*\n?\s*\.filter\(\(line\) => line\.key !== "gop"\)/);
    assert.match(script, /const activeLines = new Set\(CostData\.TOGGLEABLE_KEYS\)/);
    assert.match(script, /activeLines: Array\.from\(activeLines\)/);
    // Switching one re-renders both views from the same set.
    assert.match(script, /box\.addEventListener\("change"/);
    assert.match(html, /class="line-toggles"/);
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

// The free-text origin box offered nothing the checkboxes do not, and it sat
// directly above the travel agency subgroups: an agency name typed while looking
// for the agency search box landed in the origin list instead. An origin that is
// not in the source matches no reservation, which then silently emptied the
// matching-rate picker for that whole branch.
test("origins are picked from the source list only, with no free-text entry", () => {
    const script = read("costdata-input.js");
    const css = read("styles.css");

    assert.doesNotMatch(script, /Add an origin by name/);
    assert.doesNotMatch(script, /originDrafts/);
    assert.doesNotMatch(script, /tree-manual/);
    assert.doesNotMatch(css, /\.tree-manual/);
    // The checkboxes are still the way in.
    assert.match(script, /function originChoice\(group, value, options\)/);
});

test("an origin belongs to one group, and the others say which one has it", () => {
    const script = read("costdata-input.js");
    const service = fs.readFileSync(
        path.join(__dirname, "..", "services", "cost_settings_service.py"), "utf8"
    );

    // The same origin in two groups gives every reservation from it two fallback
    // percentages with nothing to choose between them.
    assert.match(script, /CostMatch\.originAssignmentIndex\(/);
    assert.match(script, /box\.disabled = true;/);
    assert.match(read("styles.css"), /\.is-taken-choice/);
    // And the rule is real, not only drawn: a hand-built request is rejected too.
    assert.match(service, /belongs to one group only/);
    assert.match(service, /origin_owner\[value\.casefold\(\)\] = name/);
});

test("a failed source lookup is reported as a failure, not as an empty result", () => {
    const script = read("costdata-input.js");
    const service = fs.readFileSync(
        path.join(__dirname, "..", "services", "cost_source_service.py"), "utf8"
    );

    // Both lookups used to swallow the error and return [], so a 500 read as
    // "no agency contains that" and "no reservations under these filters were
    // sold on a rate" - two statements about the property's data that were not
    // true of it.
    assert.match(script, /error: error\.message \|\| "Unknown error\."/);
    assert.match(script, /Travel agency search failed/);
    assert.match(script, /The rate lookup failed/);
    // The 500 itself: staging.travel_agency is an ETL landing table, so its key
    // is not typed like the reservation's foreign key on every deployment.
    assert.match(service, /agency\.\{agency_key\}::text = reservation\.\{agency_fk\}::text/);
    assert.match(service, /JOIN rate_current rate ON rate\.id::text = scoped\.rate_id/);
});

test("an agency term matches whatever the source calls the agency", () => {
    const script = read("costdata-input.js");
    const shared = fs.readFileSync(
        path.join(__dirname, "..", "shared", "mews_source.py"), "utf8"
    );

    // "booking.com" typed into the filter matched nothing at a property whose
    // mirror spells it "Booking com": the predicate was a substring test on the
    // raw text, so it caught only the spelling the operator happened to type.
    assert.match(shared, /def agency_fold_text\(expression\)/);
    assert.match(shared, /\[\[:space:\]\[:punct:\]\]\+/);
    // Keeping only [:alnum:] is ctype-dependent and would have cost every
    // accented name its vowels under the C locale.
    assert.doesNotMatch(shared, /\[\^\[:alnum:\]\]/);

    // And the operator is told, where they type, that it works this way - plus
    // that several terms are a union, which is the answer for an abbreviation the
    // folding cannot reach.
    // Said as a rule about every agency, with two examples: the single-example
    // wording read as a Booking.com special case.
    assert.match(script, /Capitals, spaces and punctuation are ignored for every agency/);
    assert.match(script, /expedia/);
    assert.match(script, /one per name the agency goes by/);
});

test("a term says what it matches, without the popup having to be open", () => {
    const script = read("costdata-input.js");
    const css = read("styles.css");

    // A term that catches nothing is the commonest way this rulebook quietly
    // charges the fallback percentage, and nothing on screen said so once the
    // suggestion list had closed.
    assert.match(script, /function describeMatches\(result\)/);
    assert.match(script, /function refreshSummary\(\)/);
    assert.match(script, /Matches no travel agency in this hotel's reservations/);
    assert.match(css, /\.tree-filter-match\.is-empty/);
    // Read from the cache first: every tree edit rebuilds every filter row, so a
    // request per saved term per render would be a burst on each keystroke
    // elsewhere in the tree.
    assert.match(script, /function cachedAgencies\(term, origins\)/);
    assert.match(script, /const cached = cachedAgencies\(term, originGroup\.origins\)/);
});

test("an empty matching-rate list names the filters that produced it", () => {
    const script = read("costdata-input.js");

    // It is almost always one of the filters that is too narrow, and the picker
    // never said which ones it had applied.
    assert.match(script, /function describeFilters\(\)/);
    assert.match(script, /No reservations \$\{describeFilters\(\)\} were sold on a rate/);
    assert.match(script, /lookup\.agencyFilterApplied/);
});

// This page is the only place the import can be reached from: the Function App is
// a Static Web Apps linked backend, so App Service Authentication rejects a direct
// call before the function key is read, and only a request carrying this site's
// own auth cookie gets through the proxy.
test("Cost Input can trigger the import, one dataset or all of them", () => {
    const html = read("costdata-input.html");
    const script = read("costdata-input.js");
    const css = read("styles.css");

    assert.match(html, /id="runImportButton"/);
    assert.match(html, /id="importDataset"/);
    assert.match(css, /\.settings-maintenance \{/);
    assert.match(css, /\.import-dataset select \{/);

    assert.match(script, /fetchJson\("\/api\/costdata\/import"/);
    assert.match(script, /JSON\.stringify\(\{dataset\}\)/);
    assert.match(script, /IMPORT_POLL_TIMEOUT_MS/);
    assert.match(script, /job\.status === "succeeded"/);
    assert.match(script, /job\.status === "failed"/);
    // A rejected key must not stay cached, or every later attempt fails without
    // ever asking again.
    assert.match(script, /forgetImportKey\(\)/);
    assert.match(script, /costdata-import-key/);
});

// The Function App is a Static Web Apps linked backend, so App Service
// Authentication guards it and the platform authenticates the proxied request. A
// request from this page therefore usually needs no key at all - and a WRONG one is
// worse than none, because the Functions host key check then turns down a request
// EasyAuth had already let through.
test("the import tries without a key first and only asks if refused", () => {
    const script = read("costdata-input.js");

    assert.match(script, /async function queueImport\(dataset\)/);
    assert.match(script, /return await postImport\(dataset, ""\)/);
    // The key is only attached when there is one, so the unauthenticated attempt
    // sends no header at all rather than an empty one.
    assert.match(script, /if \(key\) headers\["x-functions-key"\] = key;/);
    // Asked for after the refusal, not before it.
    assert.ok(
        script.indexOf('postImport(dataset, "")') < script.indexOf("const key = importKey()"),
        "the keyless attempt must come before the prompt"
    );
    // And when both attempts are refused, the key is only half the candidates:
    // this page sits behind the site's own login and an expired session refuses
    // the proxied request identically. Blaming the key was wrong twice over.
    assert.match(script, /session has expired/);
    assert.doesNotMatch(script, /That key was not accepted/);
    // Reloading the property list pulls the server's copy over the form, which is
    // where half an hour of unsaved work may be sitting.
    assert.match(script, /if \(dirty\) \{/);
});

test("the dataset picker offers exactly the datasets the pipeline registers", () => {
    const html = read("costdata-input.html");
    const pipeline = fs.readFileSync(
        path.join(__dirname, "..", "shared", "pipeline.py"), "utf8"
    );

    // Hand-written options against a hand-written registry drift, and a name that
    // drifts is a 400 the operator cannot act on. The API validates too, but by
    // then the button has already failed.
    const registered = [...pipeline.matchAll(/^ {4}"([a-z_]+)": \{$/gm)]
        .map(([, name]) => name);
    assert.ok(registered.length >= 8, `parsed ${registered.length} datasets`);

    // Scoped to this select. The page has three others - breakfast basis,
    // franchise basis, franchise revenue base - and matching every <option> on it
    // swept their values in too.
    const picker = /<select id="importDataset">[\s\S]*?<\/select>/.exec(html)[0];
    const offered = [...picker.matchAll(/<option value="([a-z_]+)"/g)]
        .map(([, value]) => value)
        .filter((value) => value !== "all");
    assert.deepEqual(offered, registered);
    // And "Everything", which is the API's own name for the whole run.
    assert.match(picker, /<option value="all" selected>/);
});

// The two mixes resolve their source columns at run time and SKIP rather than fail
// when they cannot, which counts as a success for the job as a whole. "Import
// complete (8 datasets)" was therefore true of a run in which the dataset you came
// here for imported nothing at all.
test("the import result names datasets that skipped or failed", () => {
    const script = read("costdata-input.js");

    assert.match(script, /function describeImport\(job\)/);
    assert.match(script, /entry\.skipped/);
    assert.match(script, /entry\.status === "failed"/);
    assert.match(script, /Skipped: /);
    // The job result is stored verbatim from the pipeline, so the keys are
    // snake_case - reading importRows or durationSeconds silently yields undefined.
    assert.match(script, /entry\.import_rows/);
    assert.match(script, /entry\.duration_seconds/);
    assert.doesNotMatch(script, /result\?\.durationSeconds/);
    // Only one cost job runs at a time, so a request during the nightly run joins
    // it rather than starting another.
    assert.match(script, /accepted\.deduplicated/);
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
    assert.match(read("costdata-cleaning.js"), /function resolveRowBed\(rowBed, bedTypes\)/);
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
    const rules = read("costdata-cleaning.js");
    const script = read("costdata-input.js");

    // The rules live in one place and are unit-tested against real bed setups
    // in costdata-cleaning.test.js; the editor only calls them.
    assert.match(rules, /function baseRowFor\(rows, categoryName\)/);
    assert.match(rules, /function resolveRow\(rows, row, bedTypes\)/);
    // Beds inherit unless the row's override is switched on; minutes inherit
    // whenever the box is empty. The two rules are deliberately different.
    assert.match(rules, /const inheritsBeds = !isBase && !row\.overridesBase/);
    assert.match(rules, /const inheritsMinutes = !isBase && isBlank\(row\.cleaningMinutes\)/);
    assert.match(script, /CostCleaning\.resolveRow\(/);
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

test("linen cost is derived from beds only, with nothing to type per row", () => {
    const script = read("costdata-input.js");
    const html = read("costdata-input.html");
    const service = fs.readFileSync(
        path.join(__dirname, "..", "services", "cost_settings_service.py"), "utf8"
    );

    // The only linen input on the page belongs to a bed type; a room row shows
    // a derived figure and offers nothing to type into.
    const cleaning = /data-settings-section="cleaning"[\s\S]*?<\/section>/.exec(html)[0];
    assert.doesNotMatch(cleaning, /name="linenCost"/);
    // The arithmetic lives in costdata-cleaning.js, which is unit-tested
    // against real bed setups; the editor only calls it.
    assert.match(read("costdata-cleaning.js"), /linen: bedsLinenCost\(beds, bedTypes\)/);
    assert.doesNotMatch(script, /numberValue\(row\.linenCost\)/);
    assert.doesNotMatch(script, /linenCost: existing/);

    // The server derives the stored column too, rather than trusting a figure
    // the client sent.
    assert.match(service, /linen = linen_of\(beds\)/);
    assert.match(service, /row\["linenCost"\] = _round_sek/);
});

// Minutes and linen were inputs into a figure nobody could see: the rate that
// prices the minutes lives at the top of the section, a long way up the page.
test("each room row prices its own minutes and totals the departure", () => {
    const script = read("costdata-input.js");
    const css = read("styles.css");

    assert.match(script, /"Guests", "Beds", "Minutes", "Staff cost", "Linen", "Total cost"/);
    assert.match(script, /function rowStaffCost\(row\)/);
    assert.match(script, /data-staff-for/);
    assert.match(script, /data-total-for/);
    // The total is the two figures beside it, not a third independent number.
    assert.match(script, /rowStaffCost\(row\) \+ state\.linen/);
    // Both move when the cost per minute at the top of the section changes, not
    // only when this row is edited.
    assert.match(script, /event\.target\.name === "cleaningCostPerMinute"/);
    assert.match(css, /\.cleaning-total \{/);
    // Six columns and the remove button, in both the desktop and mobile grids.
    const grid = /\.cleaning-grid-head,\n\.cleaning-grid-row \{[\s\S]*?\}/.exec(css)[0];
    assert.equal((grid.match(/px/g) || []).length >= 6, true, grid);
});

test("Group by sits beside the chart as well as in Query settings, on one value", () => {
    const html = read("costdata.html");
    const script = read("costdata.js");

    const panel = /id="gopChartPanel"[\s\S]*?<\/section>/.exec(html)[0];
    assert.match(panel, /id="gopChartGrain"/);
    for (const grain of ["day", "week", "month", "year"]) {
        assert.match(panel, new RegExp(`value="${grain}"`), `chart Group by missing ${grain}`);
    }
    // One value, mirrored both ways: neither control may be left showing a grain
    // that is not in force.
    assert.match(script, /function setGrain\(value\)/);
    assert.match(script, /elements\.chartGrain\.value = value/);
    assert.match(script, /elements\.grain\.value = value/);
    assert.match(read("styles.css"), /\.chart-grain \{/);
});

// ---------------------------------------------------------------------------
// Last year on the chart
// ---------------------------------------------------------------------------

test("the chart offers last year as a paired bar, with both comparison bases", () => {
    const html = read("costdata.html");
    const script = read("costdata.js");
    const css = read("styles.css");

    const panel = /id="gopChartPanel"[\s\S]*?<\/section>/.exec(html)[0];
    assert.match(panel, /id="gopChartShowLy"/);
    assert.match(panel, /id="gopChartLyBasis"/);
    // The same two values the LOS API accepts, so one vocabulary covers the
    // whole application.
    assert.match(panel, /value="sameDate"/);
    assert.match(panel, /value="sameWeekday"/);

    // Two bars in one band, this year first, each half the group.
    assert.match(script, /const barLeft = \(index, isComparison\) =>/);
    assert.match(script, /function drawStack\(period, left, palette, isComparison\)/);
    assert.match(script, /drawStack\(period, barLeft\(index, false\), CHART_COLOURS, false\)/);
    assert.match(script, /drawStack\(previous, barLeft\(index, true\), LY_COLOURS, true\)/);
    assert.match(css, /\.gop-bar-revenue\.is-comparison \{/);
});

test("last year is fetched only when it is switched on, and cached per range", () => {
    const script = read("costdata.js");

    // The page's default range is a year to date. Loading last year with every
    // update would double the cost of a query most readings never compare.
    assert.match(script, /async function ensureComparison\(\)/);
    assert.match(script, /if \(comparison && comparison\.key === key\) return;/);
    // Keyed on the range the facts on screen cover, not on what the date inputs
    // currently say - and on the basis, because a different basis is a different
    // range.
    assert.match(script, /\$\{loadedRange\.startDate\}\|\$\{loadedRange\.endDate\}\|\$\{elements\.lyBasis\.value\}/);
});

test("the two years are paired by period key, never by bar position", () => {
    const script = read("costdata.js");

    // Pairing bar 1 with bar 1 slips the moment one year has a period the other
    // does not, and then every bar after it compares against the wrong month
    // with nothing on screen to show it.
    assert.match(script, /CostData\.alignToComparison\(/);
    assert.match(
        read("costdata-data.js"), /Format\.thisYearDate\(row\.stayDate, basis\)/
    );
    assert.match(script, /comparisonPeriods\.get\(period\.periodKey\)/);
    // A period with no counterpart draws no bar: a zero-height one would claim
    // last year earned nothing, which is a different statement.
    assert.match(script, /if \(showComparison && previous\) \{/);
    // And the scale has to account for last year, or a bigger last year is drawn
    // through the top of the plot.
    assert.match(script, /previous \? \[previous\.revenue, previous\.cost\] : \[\]/);
});

test("a comparison that fails to load leaves this year's statement standing", () => {
    const script = read("costdata.js");

    // This year's figures are complete and correct; blanking the page over an
    // extra reading would be the larger loss.
    assert.match(script, /elements\.comparisonNote\.hidden = false;/);
    // Anchored on the function that performs the fetch, which is where the
    // handler lives now that the request is also started early, in parallel
    // with this year's.
    const comparisonFetch = /async function loadComparison\([\s\S]*?\n {4}\}/.exec(script);
    assert.ok(comparisonFetch, "loadComparison should own the comparison request");
    const handler = /catch \(error\) \{[\s\S]*?\n {8}\}/.exec(comparisonFetch[0]);
    assert.ok(handler, "a comparison failure should be caught, not propagated");
    assert.doesNotMatch(handler[0], /elements\.gop\.hidden = true/);
    assert.doesNotMatch(handler[0], /elements\.results\.hidden = true/);
    assert.match(read("styles.css"), /\.gop-chart-warning \{/);
});

test("the comparison request does not wait for this year's response", () => {
    const script = read("costdata.js");

    // Both are the same endpoint over ranges the date inputs already hold, so
    // fetching them one after the other put two full round trips in front of
    // one view. The comparison is started before this year's is awaited, and
    // awaited only just before the first render - so the chart is still never
    // drawn once without it and again with it.
    const load = /async function loadData\(\)[\s\S]*?\n {4}\}/.exec(script)[0];
    const started = load.indexOf("startComparison(range)");
    const awaitedMain = load.indexOf("await LosApi.fetchJson");
    const awaitedComparison = load.indexOf("await comparisonRequest");
    const rendered = load.indexOf("render();");

    assert.ok(started !== -1, "the comparison should be started inside loadData");
    assert.ok(started < awaitedMain, "it should be started before this year's is awaited");
    assert.ok(awaitedMain < awaitedComparison, "and awaited afterwards");
    assert.ok(awaitedComparison < rendered, "but before the first render");
});

test("a missing control warns instead of killing the page bootstrap", () => {
    const script = read("costdata-input.js");

    // These lookups run before loadHotels(), so a null threw and left the page
    // stuck on "Loading properties..." - which is what a half-deployed
    // HTML/JS pair looked like.
    assert.match(script, /function onClick\(selector, handler\)/);
    assert.doesNotMatch(script, /document\.querySelector\("\[data-add-[^"]*\]"\)\.onclick/);
});
