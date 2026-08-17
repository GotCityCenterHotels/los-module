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

    /**
     * Which rate group in the distribution tree already claims each rate.
     *
     * The same problem as assignmentIndex, one level deeper: a rate sitting in
     * two rate groups has two distribution percentages and no way to choose
     * between them. The owner label is the full path, because "Corporate" alone
     * does not say which origin group's Corporate it is.
     *
     * `except` is the rate group being edited, as {originIndex, agencyIndex,
     * rateGroupIndex}; it never reports itself as a conflict.
     */
    function rateAssignmentIndex(originGroups, except) {
        const assigned = new Map();
        const skip = except || {};
        (originGroups || []).forEach((origin, originIndex) => {
            const originName = String(origin.groupName || "").trim()
                || `Group ${originIndex + 1}`;
            (origin.agencyGroups || []).forEach((agency, agencyIndex) => {
                const agencyName = String(agency.groupName || "").trim()
                    || `Subgroup ${agencyIndex + 1}`;
                (agency.rateGroups || []).forEach((rateGroup, rateGroupIndex) => {
                    if (originIndex === skip.originIndex
                        && agencyIndex === skip.agencyIndex
                        && rateGroupIndex === skip.rateGroupIndex) return;
                    const rateGroupName = String(rateGroup.groupName || "").trim()
                        || `Rates ${rateGroupIndex + 1}`;
                    for (const rate of rateGroup.rates || []) {
                        const name = String(rate.rateName || "").trim();
                        if (!name) continue;
                        assigned.set(
                            name.toLowerCase(),
                            `${originName} / ${agencyName} / ${rateGroupName}`
                        );
                    }
                });
            });
        });
        return assigned;
    }

    /**
     * Which origin group already claims each reservation origin.
     *
     * An origin belongs to exactly one origin group. The same origin in two
     * groups gives every reservation from it two fallback percentages with
     * nothing to choose between them, so the picker greys it out in the groups
     * that do not own it and the server rejects it outright.
     *
     * `exceptOriginIndex` is the group being drawn; it never reports its own
     * origins as taken.
     */
    function originAssignmentIndex(originGroups, exceptOriginIndex) {
        const assigned = new Map();
        (originGroups || []).forEach((group, originIndex) => {
            if (originIndex === exceptOriginIndex) return;
            const owner = String(group.groupName || "").trim()
                || `Group ${originIndex + 1}`;
            for (const origin of group.origins || []) {
                const value = String(origin || "").trim();
                if (!value) continue;
                assigned.set(value.toLowerCase(), owner);
            }
        });
        return assigned;
    }

    const api = {
        assignmentIndex, partitionOptions, rateAssignmentIndex,
        originAssignmentIndex
    };

    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }

    root.CostMatch = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
