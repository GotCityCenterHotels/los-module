(function initializeCostCleaning(root) {
    "use strict";

    /**
     * The cleaning rulebook's arithmetic, with no DOM in it.
     *
     * A property defines its bed types once, each with the linen cost of making
     * it up. A room category then says which beds are made up at each guest
     * count, and the linen cost of a row is the sum of those beds - so a family
     * room made up with one double and two singles costs one double's linen
     * plus twice a single's.
     *
     * Most categories are made up the same way whatever the guest count, so the
     * lowest count carries the setup and the counts above it inherit: beds
     * unless that row's own override is switched on, minutes whenever that
     * row's box is left empty. The two rules differ deliberately - switching an
     * override on says "this count is made up differently", while leaving a
     * minutes box empty is just not having typed a number.
     *
     * This lives apart from the editor because it is the half that has to be
     * right: services/cost_settings_service.py resolves the same rules on the
     * way in and out of the database, and the two must agree or the figure the
     * editor shows is not the figure the Cost Data page costs.
     */

    function numberValue(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function sameName(left, right) {
        return String(left || "").trim().toLowerCase()
            === String(right || "").trim().toLowerCase();
    }

    /**
     * The bed type a room row points at.
     *
     * By key first: the key is the editor's stable handle on a bed type, and
     * the name is a thing the operator edits one keystroke at a time. Matching
     * on the name is the fallback for a row that has no key yet - a model
     * straight from the database, where only names exist.
     */
    function resolveRowBed(rowBed, bedTypes) {
        const beds = bedTypes || [];
        const bed = (rowBed.bedKey
            && beds.find((entry) => entry.bedKey === rowBed.bedKey))
            || beds.find((entry) => sameName(entry.bedName, rowBed.bedName))
            || null;
        return {
            bed,
            name: bed ? bed.bedName : rowBed.bedName,
            linenCost: bed ? numberValue(bed.linenCost) : 0
        };
    }

    /** What making up this set of beds costs in linen. */
    function bedsLinenCost(beds, bedTypes) {
        return (beds || []).reduce(
            (total, bed) => total
                + resolveRowBed(bed, bedTypes).linenCost
                * Math.max(1, numberValue(bed.quantity) || 1),
            0
        );
    }

    /** The row that carries a category's setup: its lowest guest count. */
    function baseRowFor(rows, categoryName) {
        let base = null;
        for (const row of rows || []) {
            if (!sameName(row.categoryName, categoryName)) continue;
            if (!base || Number(row.occupancy) < Number(base.occupancy)) base = row;
        }
        return base;
    }

    function isBlank(value) {
        return value === null || value === undefined || value === "";
    }

    /** One row's effective setup, after inheritance. */
    function resolveRow(rows, row, bedTypes) {
        const base = baseRowFor(rows, row.categoryName);
        const isBase = base === row;
        const inheritsBeds = !isBase && !row.overridesBase;
        const beds = ((inheritsBeds ? base : row) || {}).beds || [];
        const inheritsMinutes = !isBase && isBlank(row.cleaningMinutes);
        const minutes = inheritsMinutes
            ? (base ? base.cleaningMinutes : null)
            : row.cleaningMinutes;
        return {
            base,
            isBase,
            inheritsBeds,
            inheritsMinutes,
            beds,
            minutes,
            // Linen is the beds and nothing else. A row with no beds costs no
            // linen; there is no typed figure to fall back on.
            linen: bedsLinenCost(beds, bedTypes)
        };
    }

    const api = {numberValue, resolveRowBed, bedsLinenCost, baseRowFor, resolveRow};

    if (typeof module === "object" && module.exports) module.exports = api;
    root.CostCleaning = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
