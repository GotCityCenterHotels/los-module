const test = require("node:test");
const assert = require("node:assert/strict");

const CostMatch = require("../frontend/costdata-match.js");

const RATES = [
    {id: "r1", name: "BAR"},
    {id: "r2", name: "Corporate"},
    {id: "r3", name: "Non-refundable"}
];

function groups() {
    return [
        {groupName: "OTA", rules: [{matchType: "rate", matchValue: "BAR"}]},
        {groupName: "Direct", rules: [{matchType: "rate", matchValue: "Corporate"}]}
    ];
}

test("a rate used by another group is reported with that group's name", () => {
    const assigned = CostMatch.assignmentIndex(groups(), "rate", null, null);
    assert.equal(assigned.get("bar"), "OTA");
    assert.equal(assigned.get("corporate"), "Direct");
});

test("the rule being edited does not count as a conflict with itself", () => {
    // Editing OTA's own BAR row must not flag BAR as taken.
    const assigned = CostMatch.assignmentIndex(groups(), "rate", 0, 0);
    assert.equal(assigned.has("bar"), false);
    assert.equal(assigned.get("corporate"), "Direct");
});

test("assignments are matched case-insensitively", () => {
    const withCasing = [{groupName: "OTA", rules: [{matchType: "rate", matchValue: "  bAr "}]}];
    const assigned = CostMatch.assignmentIndex(withCasing, "rate", null, null);
    assert.equal(assigned.get("bar"), "OTA");
});

test("channel assignments never collide with rate assignments", () => {
    const mixed = [{groupName: "OTA", rules: [{matchType: "channel", matchValue: "BAR"}]}];
    assert.equal(CostMatch.assignmentIndex(mixed, "rate", null, null).size, 0);
    assert.equal(CostMatch.assignmentIndex(mixed, "channel", null, null).get("bar"), "OTA");
});

test("a group with no name falls back to its position", () => {
    const unnamed = [{groupName: "  ", rules: [{matchType: "rate", matchValue: "BAR"}]}];
    assert.equal(CostMatch.assignmentIndex(unnamed, "rate", null, null).get("bar"), "Group 1");
});

test("assigned rates are separated from selectable ones and carry their group", () => {
    const assigned = CostMatch.assignmentIndex(groups(), "rate", null, null);
    const {free, taken} = CostMatch.partitionOptions(RATES, assigned, "");

    assert.deepEqual(free.map(item => item.name), ["Non-refundable"]);
    assert.deepEqual(taken, [
        {name: "BAR", owner: "OTA"},
        {name: "Corporate", owner: "Direct"}
    ]);
});

test("search filters both sections", () => {
    const assigned = CostMatch.assignmentIndex(groups(), "rate", null, null);
    const {free, taken} = CostMatch.partitionOptions(RATES, assigned, "cor");

    assert.deepEqual(free, []);
    assert.deepEqual(taken, [{name: "Corporate", owner: "Direct"}]);
});

test("search is case-insensitive and matches anywhere in the name", () => {
    const {free} = CostMatch.partitionOptions(RATES, new Map(), "REFUND");
    assert.deepEqual(free.map(item => item.name), ["Non-refundable"]);
});

test("with nothing assigned every rate stays selectable", () => {
    const {free, taken} = CostMatch.partitionOptions(RATES, new Map(), "");
    assert.equal(free.length, 3);
    assert.equal(taken.length, 0);
});

test("an empty source list yields no options rather than throwing", () => {
    const {free, taken} = CostMatch.partitionOptions([], new Map(), "bar");
    assert.deepEqual(free, []);
    assert.deepEqual(taken, []);
});

test("blank match values are ignored when indexing assignments", () => {
    const blanks = [{groupName: "OTA", rules: [{matchType: "rate", matchValue: "   "}]}];
    assert.equal(CostMatch.assignmentIndex(blanks, "rate", null, null).size, 0);
});

test("the picker renders assigned options last so they sit at the bottom", () => {
    // Order matters: the rendered list concatenates free then taken.
    const assigned = CostMatch.assignmentIndex(groups(), "rate", null, null);
    const {free, taken} = CostMatch.partitionOptions(RATES, assigned, "");
    const rendered = free.concat(taken).map(item => item.name);
    assert.deepEqual(rendered, ["Non-refundable", "BAR", "Corporate"]);
});

// ---------------------------------------------------------------------------
// The three-level distribution tree
// ---------------------------------------------------------------------------

function tree() {
    return [
        {
            groupName: "Channel manager",
            origins: ["ChannelManager"],
            agencyGroups: [
                {
                    groupName: "Expedia",
                    filters: [{matchField: "travelAgency", containsValue: "expedia"}],
                    rateGroups: [
                        {groupName: "Package", costPercent: 12, rates: [
                            {rateId: "r1", rateName: "BAR"},
                            {rateId: null, rateName: "Corporate"}
                        ]}
                    ]
                }
            ]
        },
        {
            groupName: "Direct",
            origins: ["Commander"],
            agencyGroups: [
                {
                    groupName: "  ",
                    filters: [],
                    rateGroups: [
                        {groupName: "", costPercent: 0, rates: [
                            {rateId: null, rateName: "Non-refundable"}
                        ]}
                    ]
                }
            ]
        }
    ];
}

test("a rate already in the tree names the whole path that owns it", () => {
    const assigned = CostMatch.rateAssignmentIndex(tree(), {});

    assert.equal(assigned.get("bar"), "Channel manager / Expedia / Package");
    // Unnamed levels fall back to their position, so the message still says
    // where to look rather than reading "  /  / ".
    assert.equal(assigned.get("non-refundable"), "Direct / Subgroup 1 / Rates 1");
});

test("the rate group being edited does not report itself as a conflict", () => {
    const editing = {originIndex: 0, agencyIndex: 0, rateGroupIndex: 0};
    const assigned = CostMatch.rateAssignmentIndex(tree(), editing);

    assert.equal(assigned.has("bar"), false);
    assert.equal(assigned.has("corporate"), false);
    // A different branch is still a conflict.
    assert.equal(assigned.get("non-refundable"), "Direct / Subgroup 1 / Rates 1");
});

test("tree rate assignments are matched case-insensitively and skip blanks", () => {
    const messy = [{
        groupName: "OTA",
        agencyGroups: [{groupName: "All", rateGroups: [
            {groupName: "Rates", rates: [{rateName: "  bAr "}, {rateName: "   "}]}
        ]}]
    }];
    const assigned = CostMatch.rateAssignmentIndex(messy, {});

    assert.equal(assigned.get("bar"), "OTA / All / Rates");
    assert.equal(assigned.size, 1);
});

test("the same partition powers the tree picker, so taken rates sort last", () => {
    // Editing the Expedia package group frees its own two rates; the one held
    // by the other branch stays taken and drops to the bottom of the list.
    const assigned = CostMatch.rateAssignmentIndex(
        tree(), {originIndex: 0, agencyIndex: 0, rateGroupIndex: 0}
    );
    const {free, taken} = CostMatch.partitionOptions(RATES, assigned, "");

    assert.deepEqual(free.concat(taken).map(item => item.name), [
        "BAR", "Corporate", "Non-refundable"
    ]);
    assert.deepEqual(taken, [
        {name: "Non-refundable", owner: "Direct / Subgroup 1 / Rates 1"}
    ]);
});
