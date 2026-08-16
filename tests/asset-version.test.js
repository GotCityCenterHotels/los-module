const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {contentHash, stamp, pages} = require("../scripts/stamp-assets.js");

const frontend = path.join(__dirname, "..", "frontend");
const read = (file) => fs.readFileSync(path.join(frontend, file), "utf8");

// There is no build step: the HTML hand-writes the `?v=` token a browser
// caches against. A hand-maintained token gets bumped once per batch of edits,
// so anything changed after the bump ships under a token the browser already
// holds and the page silently keeps running the previous script. That has
// already happened once - a rewritten Cost Input shipped under a token from
// before the rewrite, and the new controls simply did nothing, with no error
// anywhere to explain it.
//
// Stamping is mechanical, so it is not left to anyone to remember: package.json
// runs it as `pretest`, which means it happens before every local test run and
// before the deploy workflow's own `npm test`. This test is the assertion that
// it did - it should never fail on its own, and if it does, the pretest hook
// has been removed or the stamper is broken.
test("every asset reference carries a hash of the file it points at", () => {
    for (const page of pages()) {
        assert.equal(
            stamp(read(page)),
            read(page),
            `${page} references a stale asset version. `
                + "Run: npm run stamp (this normally happens automatically)"
        );
    }
});

test("a changed file changes its token, so no two versions share one", () => {
    const before = contentHash(path.join(frontend, "costdata-input.js"));
    const scratch = path.join(frontend, "__version-probe.js");
    try {
        fs.writeFileSync(scratch, "// one\n");
        const one = contentHash(scratch);
        fs.writeFileSync(scratch, "// two\n");
        assert.notEqual(one, contentHash(scratch));
    }
    finally {
        fs.rmSync(scratch, {force: true});
    }
    // And hashing is stable: an unchanged file keeps its token, so stamping is
    // not a source of spurious diffs.
    assert.equal(before, contentHash(path.join(frontend, "costdata-input.js")));
});

test("every referenced asset actually exists in the frontend directory", () => {
    for (const page of pages()) {
        const html = read(page);
        for (const [, , asset] of html.matchAll(
            /(src|href)="([^"?]+\.(?:js|css))(?:\?v=[^"]*)?"/g
        )) {
            if (asset.startsWith("http") || asset.startsWith("//")) continue;
            assert.ok(
                fs.existsSync(path.join(frontend, asset)),
                `${page} references ${asset}, which does not exist`
            );
        }
    }
});
