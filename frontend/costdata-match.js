(function initializeCostMatch(root) {
    "use strict";

    /**
     * Which distribution group each match value is already assigned to.
     *
     * The rule being edited is excluded so a row does not report itself as a
     * conflict. Comparison is case-insensitive because the values are typed by
     * hand as often as they are picked.
     */
    function assignmentIndex(groups, matchType, exceptGroupIndex, exceptRuleIndex) {
        const assigned = new Map();
        (groups || []).forEach((group, groupIndex) => {
            (group.rules || []).forEach((rule, ruleIndex) => {
                if (rule.matchType !== matchType) return;
                if (groupIndex === exceptGroupIndex && ruleIndex === exceptRuleIndex) return;
                const value = String(rule.matchValue || "").trim();
                if (!value) return;
                assigned.set(
                    value.toLowerCase(),
                    String(group.groupName || "").trim() || `Group ${groupIndex + 1}`
                );
            });
        });
        return assigned;
    }

    /**
     * Split a hotel's rates or channels into what can still be picked and what
     * is already taken.
     *
     * "taken" is returned separately so the picker can render it as its own
     * section at the bottom of the list, greyed out and labelled with the group
     * that owns it. The same value in two groups would make the cost percentage
     * ambiguous, so those entries must not be selectable.
     */
    function partitionOptions(available, assigned, searchTerm) {
        const term = String(searchTerm || "").trim().toLowerCase();
        const free = [];
        const taken = [];
        for (const item of available || []) {
            const name = String(item.name || "");
            if (term && !name.toLowerCase().includes(term)) continue;
            const owner = assigned instanceof Map
                ? assigned.get(name.toLowerCase())
                : (assigned || {})[name.toLowerCase()];
            if (owner) taken.push({name, owner});
            else free.push({name});
        }
        return {free, taken};
    }

    const api = {assignmentIndex, partitionOptions};

    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }

    root.CostMatch = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
