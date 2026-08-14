const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(
    path.join(__dirname, "..", "frontend", "costdata-input.js"),
    "utf8"
);

test("cost import uses one asynchronous all-dataset job", () => {
    assert.match(source, /JSON\.stringify\(\{dataset:"all"\}\)/);
    assert.match(source, /accepted\.statusUrl/);
    assert.doesNotMatch(source, /IMPORT_DATASETS/);
});

test("cost import polling handles terminal job states", () => {
    assert.match(source, /job\.status === "succeeded"/);
    assert.match(source, /job\.status === "failed"/);
    assert.match(source, /IMPORT_POLL_TIMEOUT_MS/);
});
