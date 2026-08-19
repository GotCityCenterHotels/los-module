const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {execFileSync} = require("node:child_process");

const root = path.join(__dirname, "..");
const frontend = path.join(root, "frontend");
const dist = path.join(root, "dist");

// The build is in the deploy path now, so it needs the same standard of proof as
// the code it minifies. The risk is specific and worth naming: these are classic
// scripts that assign to globals and are loaded with plain <script> tags, so a
// minifier that wrapped a file in a closure or renamed a top-level binding would
// break every page at once, silently, and only in production.
//
// esbuild is a devDependency, so a bare checkout without `npm ci` cannot run
// these. Skipping is correct there - it is not a failure of the build.
let esbuildAvailable = true;
try {
    require.resolve("esbuild");
}
catch {
    esbuildAvailable = false;
}

const options = {skip: esbuildAvailable ? false : "esbuild not installed (npm ci)"};

function build() {
    execFileSync(process.execPath, [path.join(root, "scripts", "build-frontend.js")], {
        cwd: root,
        stdio: ["ignore", "ignore", "inherit"],
    });
}

test("the build minifies every asset and carries everything else across", options, () => {
    build();

    for (const entry of fs.readdirSync(frontend)) {
        assert.ok(
            fs.existsSync(path.join(dist, entry)),
            `dist/ is missing ${entry}`
        );
    }

    // staticwebapp.config.json holds the CSP and the cache headers. Deploying
    // dist/ without it would serve the whole site unprotected and uncached.
    assert.ok(fs.existsSync(path.join(dist, "staticwebapp.config.json")));
});

test("minified assets are smaller, and the stylesheet fits one congestion window", options, () => {
    build();

    const before = fs.statSync(path.join(frontend, "styles.css")).size;
    const after = fs.statSync(path.join(dist, "styles.css")).size;
    assert.ok(after < before, "styles.css was not minified");

    // The reason this build exists: render-blocking CSS above ~14.6KB on the wire
    // costs a second round trip before first paint.
    const gzipped = require("node:zlib")
        .gzipSync(fs.readFileSync(path.join(dist, "styles.css")), {level: 9}).length;
    assert.ok(
        gzipped < 14_600,
        `dist/styles.css is ${gzipped} bytes gzipped, over the initial window`
    );
});

test("every minified script still parses and keeps its global", options, () => {
    build();

    // A module that lost its global would still parse, so parsing is not enough.
    const globals = {
        "los-api.js": "LosApi",
        "los-data.js": "LosData",
        "los-format.js": "LosFormat",
    };

    for (const file of fs.readdirSync(dist).filter((name) => name.endsWith(".js"))) {
        const full = path.join(dist, file);
        execFileSync(process.execPath, ["--check", full], {stdio: ["ignore", "ignore", "inherit"]});

        const exported = globals[file];
        if (!exported) continue;
        assert.match(
            fs.readFileSync(full, "utf8"),
            new RegExp(exported),
            `${file} no longer assigns ${exported}`
        );
    }
});

test("the minified modules behave identically to their sources", options, () => {
    build();

    // The strongest check available without a browser: load the minified module
    // and run the same assertions the source is held to.
    const sourceData = require(path.join(frontend, "los-data.js"));
    delete require.cache[require.resolve(path.join(dist, "los-data.js"))];
    const minifiedData = require(path.join(dist, "los-data.js"));

    const facts = [
        {arrivalDate: "2026-01-05", hotelName: "A", scenario: "current", los: 2, bookingCount: 3, nightCount: 6},
        {arrivalDate: "2026-01-19", hotelName: "A", scenario: "current", los: 5, bookingCount: 1, nightCount: 5}
    ];
    const asSource = sourceData.calculateAverageView(facts, {grain: "month"});
    const asMinified = minifiedData.calculateAverageView(facts, {grain: "month"});
    assert.deepEqual(asMinified.rows, asSource.rows);
    assert.deepEqual(asMinified.summaryRows, asSource.summaryRows);

    const sourceApi = require(path.join(frontend, "los-api.js"));
    delete require.cache[require.resolve(path.join(dist, "los-api.js"))];
    const minifiedApi = require(path.join(dist, "los-api.js"));
    assert.deepEqual(
        minifiedApi.buildContiguousMonthRanges(["2026-02", "2026-03"], "2026-01-01", "2026-12-31"),
        sourceApi.buildContiguousMonthRanges(["2026-02", "2026-03"], "2026-01-01", "2026-12-31")
    );
});

test("dist pages are stamped against the minified bytes, not the sources", options, () => {
    build();

    const {contentHash} = require("../scripts/stamp-assets.js");

    for (const page of fs.readdirSync(dist).filter((name) => name.endsWith(".html"))) {
        const html = fs.readFileSync(path.join(dist, page), "utf8");
        for (const [, , asset, token] of html.matchAll(
            /(src|href)="([^"?]+\.(?:js|css))\?v=([^"]*)"/g
        )) {
            assert.equal(
                token,
                contentHash(path.join(dist, asset)),
                `${page} stamps ${asset} with a token that is not its dist hash`
            );
        }
    }
});
