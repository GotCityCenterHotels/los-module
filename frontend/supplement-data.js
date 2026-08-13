(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    else root.SupplementData = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";

    const DAY_MS = 86_400_000;
    const MAX_RANGE_DAYS = 366;
    const METRICS = Object.freeze(["occ", "adr", "revpar"]);

    function parseDateKey(value) {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value || ""))) return null;
        const parsed = new Date(`${value}T00:00:00Z`);
        return Number.isNaN(parsed.getTime()) || formatDateKey(parsed) !== value ? null : parsed;
    }

    function formatDateKey(date) {
        return date.toISOString().slice(0, 10);
    }

    function validateDateRange(startDate, endDate) {
        const start = parseDateKey(startDate);
        const end = parseDateKey(endDate);
        if (!start || !end) return { valid: false, error: "Enter a valid start and end date.", dayCount: 0 };
        const dayCount = Math.round((end - start) / DAY_MS) + 1;
        if (dayCount < 1) return { valid: false, error: "Start date cannot be after end date.", dayCount };
        if (dayCount > MAX_RANGE_DAYS) return {
            valid: false,
            error: `Supplement ranges are limited to ${MAX_RANGE_DAYS} days.`,
            dayCount
        };
        return { valid: true, error: null, dayCount };
    }

    function calculateDifference(current, comparison, differenceMode) {
        if (!Number.isFinite(current) || !Number.isFinite(comparison)) return null;
        if (differenceMode === "currency") return current - comparison;
        return comparison === 0 ? null : (current - comparison) / Math.abs(comparison) * 100;
    }

    function formatMetric(value, metric) {
        if (!Number.isFinite(value)) return "–";
        if (metric === "occ") return `${value.toFixed(1)}%`;
        return new Intl.NumberFormat("en-SE", { maximumFractionDigits: 0 }).format(value);
    }

    function formatDifference(value, mode, metric) {
        if (!Number.isFinite(value)) return "–";
        const sign = value > 0 ? "+" : "";
        if (mode === "percent") return `${sign}${value.toFixed(1)}%`;
        const suffix = metric === "occ" ? " pp" : " kr";
        return `${sign}${new Intl.NumberFormat("en-SE", { maximumFractionDigits: 1 }).format(value)}${suffix}`;
    }

    function computeRowAverages(row) {
        const output = {};
        for (const mode of ["today", "spit", "ly"]) {
            const facts = row.cells.reduce((total, cell) => ({
                assignedRooms: total.assignedRooms + (cell[mode]?.assignedRooms || 0),
                revenue: total.revenue + (cell[mode]?.revenue || 0),
                inventory: total.inventory + (cell[mode]?.inventory || 0)
            }), { assignedRooms: 0, revenue: 0, inventory: 0 });
            output[mode] = {
                occ: facts.inventory > 0 ? facts.assignedRooms / facts.inventory * 100 : null,
                adr: facts.assignedRooms > 0 ? facts.revenue / facts.assignedRooms : null,
                revpar: facts.inventory > 0 ? facts.revenue / facts.inventory : null
            };
        }
        return output;
    }

    async function fetchJson(url) {
        const response = await fetch(url, { headers: { Accept: "application/json" } });
        let payload;
        try {
            payload = await response.json();
        } catch (_error) {
            payload = null;
        }
        if (!response.ok) {
            const error = new Error(payload?.error || `Supplement request failed (${response.status})`);
            error.status = response.status;
            throw error;
        }
        return payload;
    }

    function queryString(parameters) {
        const query = new URLSearchParams();
        for (const [key, value] of Object.entries(parameters)) {
            if (Array.isArray(value)) {
                if (value.length) query.set(key, value.join(","));
            } else if (value !== null && value !== undefined && value !== "") query.set(key, value);
        }
        return query.toString();
    }

    function fetchMetadata(apiBaseUrl = "/api") {
        return fetchJson(`${apiBaseUrl}/supplement/hotels`);
    }

    function fetchGrid(parameters, apiBaseUrl = "/api") {
        return fetchJson(`${apiBaseUrl}/supplement/grid?${queryString(parameters)}`);
    }

    function fetchDetail(parameters, apiBaseUrl = "/api") {
        return fetchJson(`${apiBaseUrl}/supplement/detail?${queryString(parameters)}`);
    }

    return {
        MAX_RANGE_DAYS,
        METRICS,
        parseDateKey,
        formatDateKey,
        validateDateRange,
        calculateDifference,
        formatMetric,
        formatDifference,
        computeRowAverages,
        fetchMetadata,
        fetchGrid,
        fetchDetail
    };
}));
