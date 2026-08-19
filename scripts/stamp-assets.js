#!/usr/bin/env node
/**
 * Stamp every local script and stylesheet reference with a hash of its own
 * contents.
 *
 * There is no build step here: the HTML hand-writes `?v=` tokens and a browser
 * caches whatever it was served under that token. A hand-maintained token gets
 * bumped when someone remembers, which is exactly once per batch of edits - so
 * a file changed after the bump ships under a token the browser already has,
 * and the page silently keeps running the previous version. That failure has
 * no symptom other than the feature appearing not to work.
 *
 * Deriving the token from the content removes the judgement call. Run this
 * before committing frontend changes; tests/asset-version.test.js fails if it
 * has not been run.
 *
 *   node scripts/stamp-assets.js          rewrite the HTML
 *   node scripts/stamp-assets.js --check   report drift, change nothing
 *
 * The directory is a parameter because scripts/build-frontend.js re-stamps its
 * minified copy in dist/, where the tokens have to name the minified bytes rather
 * than the sources a browser is never served. Everything here defaults to
 * frontend/, which is what the CLI and the tests use.
 */
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const frontend = path.join(__dirname, "..", "frontend");
const REFERENCE = /(src|href)="([^"?]+\.(?:js|css))(\?v=[^"]*)?"/g;

function contentHash(file) {
    return crypto
        .createHash("sha256")
        .update(fs.readFileSync(file))
        .digest("hex")
        .slice(0, 10);
}

function stamp(html, directory = frontend) {
    return html.replace(REFERENCE, (whole, attribute, asset) => {
        const target = path.join(directory, asset);
        // Anything not shipped from this directory is left exactly as it is.
        if (!fs.existsSync(target)) return whole;
        return `${attribute}="${asset}?v=${contentHash(target)}"`;
    });
}

function pages(directory = frontend) {
    return fs.readdirSync(directory).filter((file) => file.endsWith(".html"));
}

/** Rewrite every page in one directory against its own assets. */
function stampDirectory(directory) {
    const rewritten = [];
    for (const page of pages(directory)) {
        const file = path.join(directory, page);
        const before = fs.readFileSync(file, "utf8");
        const after = stamp(before, directory);
        if (before === after) continue;
        fs.writeFileSync(file, after);
        rewritten.push(page);
    }
    return rewritten;
}

function main() {
    const checkOnly = process.argv.includes("--check");
    const stale = [];
    for (const page of pages()) {
        const file = path.join(frontend, page);
        const before = fs.readFileSync(file, "utf8");
        const after = stamp(before);
        if (before === after) continue;
        stale.push(page);
        if (!checkOnly) fs.writeFileSync(file, after);
    }
    if (!stale.length) {
        console.log("Asset versions are current.");
        return 0;
    }
    if (checkOnly) {
        console.error(
            `Asset versions are stale in: ${stale.join(", ")}\n`
            + "Run: node scripts/stamp-assets.js"
        );
        return 1;
    }
    console.log(`Stamped: ${stale.join(", ")}`);
    return 0;
}

if (require.main === module) process.exit(main());
module.exports = {contentHash, stamp, pages, stampDirectory};
