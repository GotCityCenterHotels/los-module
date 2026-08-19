#!/usr/bin/env node
/**
 * Produce the deployable frontend: the same files, minified, re-stamped.
 *
 * Why this exists at all, given stamp-assets.js says there is no build step:
 * styles.css is render-blocking on all five pages and was 19.8KB gzipped, which
 * overflows the ~14.6KB a server can send in the initial congestion window. That
 * costs a second round trip before the first paint on every cold-cache visit.
 * Minified it is 11.6KB and fits in one. The scripts come down 43-51% on the same
 * measure.
 *
 * It writes to dist/ rather than minifying frontend/ in place, so the sources
 * stay readable and there is no generated artifact to keep in sync or to commit
 * by accident. dist/ is gitignored and rebuilt from scratch every run.
 *
 * Stamping happens LAST, against the minified bytes. A token derived from the
 * unminified file would name content no browser is ever served, which defeats the
 * point of deriving it from content at all.
 *
 *   node scripts/build-frontend.js
 */
const fs = require("node:fs");
const path = require("node:path");

const {stampDirectory} = require("./stamp-assets.js");

const root = path.join(__dirname, "..");
const source = path.join(root, "frontend");
const output = path.join(root, "dist");

function loadEsbuild() {
    try {
        return require("esbuild");
    }
    catch {
        throw new Error("esbuild is not installed. Run: npm ci");
    }
}

function build() {
    const esbuild = loadEsbuild();

    fs.rmSync(output, {recursive: true, force: true});
    fs.mkdirSync(output, {recursive: true});

    const entries = fs.readdirSync(source, {withFileTypes: true});
    const minified = [];
    const copied = [];

    for (const entry of entries) {
        if (!entry.isFile()) continue;
        const from = path.join(source, entry.name);
        const to = path.join(output, entry.name);
        const isScript = entry.name.endsWith(".js");
        const isStyle = entry.name.endsWith(".css");

        if (!isScript && !isStyle) {
            // HTML, staticwebapp.config.json, and anything added here later.
            fs.copyFileSync(from, to);
            copied.push(entry.name);
            continue;
        }

        // transform, deliberately, not build. These are classic scripts rather
        // than modules: they assign to globals (window.LosApi, window.LosData)
        // and the pages load them with plain <script> tags. transform minifies
        // one file in isolation - it never bundles, never wraps the body in a
        // closure, and never renames a top-level name - so those globals survive.
        // build() with bundling would rename them and break every page at once.
        const result = esbuild.transformSync(
            fs.readFileSync(from, "utf8"),
            {minify: true, loader: isScript ? "js" : "css", sourcefile: entry.name}
        );
        for (const warning of result.warnings || []) {
            console.warn(`${entry.name}: ${warning.text}`);
        }
        fs.writeFileSync(to, result.code);
        minified.push(entry.name);
    }

    const stamped = stampDirectory(output);
    const weigh = (directory, file) =>
        fs.statSync(path.join(directory, file)).size;

    console.log(
        `Built dist/: ${minified.length} minified, ${copied.length} copied, `
            + `${stamped.length} page(s) stamped.`
    );
    console.log(
        `  styles.css ${weigh(source, "styles.css")} -> `
            + `${weigh(output, "styles.css")} bytes`
    );
    return 0;
}

if (require.main === module) {
    try {
        process.exit(build());
    }
    catch (error) {
        console.error(error.message);
        process.exit(1);
    }
}

module.exports = {build};
