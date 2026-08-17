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

    // Constructing an Intl.NumberFormat is expensive - it resolves a locale and
    // builds a formatter - and the grid calls these once per cell. A 31 day
    // window is several thousand cells per render, so building the formatter
    // once instead of once per number is worth more here than anything else in
    // the render path.
    const wholeNumberFormat = new Intl.NumberFormat("en-SE", { maximumFractionDigits: 0 });
    const oneDecimalFormat = new Intl.NumberFormat("en-SE", { maximumFractionDigits: 1 });

    function formatMetric(value, metric) {
        if (!Number.isFinite(value)) return "–";
        if (metric === "occ") return `${value.toFixed(1)}%`;
        return wholeNumberFormat.format(value);
    }

    function formatDifference(value, mode, metric) {
        if (!Number.isFinite(value)) return "–";
        const sign = value > 0 ? "+" : "";
        if (mode === "percent") return `${sign}${value.toFixed(1)}%`;
        const suffix = metric === "occ" ? " pp" : " kr";
        return `${sign}${oneDecimalFormat.format(value)}${suffix}`;
    }

    const CELL_MODES = Object.freeze(["today", "spit", "ly"]);

    function deriveMetrics(assignedRooms, revenue, inventory) {
        return {
            occ: inventory > 0 ? assignedRooms / inventory * 100 : null,
            adr: assignedRooms > 0 ? revenue / assignedRooms : null,
            revpar: inventory > 0 ? revenue / inventory : null,
            assignedRooms,
            revenue,
            inventory
        };
    }

    function computeRowAverages(row) {
        const output = {};
        for (const mode of CELL_MODES) {
            let assignedRooms = 0;
            let revenue = 0;
            let inventory = 0;
            for (const cell of row.cells) {
                const facts = cell[mode];
                if (!facts) continue;
                assignedRooms += facts.assignedRooms || 0;
                revenue += facts.revenue || 0;
                inventory += facts.inventory || 0;
            }
            output[mode] = deriveMetrics(assignedRooms, revenue, inventory);
        }
        return output;
    }

    /**
     * The total row for a subset of the published rows.
     *
     * Cells carry the additive facts as well as the ratios, so a subtotal is
     * exactly the sum of its parts re-divided - the same weighting the server
     * applies when it builds its own total row. That is what lets a room
     * category be switched off without asking the server for a new grid.
     */
    function sumRowCells(rows, dateCount) {
        const cells = new Array(dateCount);
        for (let index = 0; index < dateCount; index += 1) {
            const cell = {};
            for (const mode of CELL_MODES) {
                let assignedRooms = 0;
                let revenue = 0;
                let inventory = 0;
                for (const row of rows) {
                    const facts = row.cells[index]?.[mode];
                    if (!facts) continue;
                    assignedRooms += facts.assignedRooms || 0;
                    revenue += facts.revenue || 0;
                    inventory += facts.inventory || 0;
                }
                cell[mode] = deriveMetrics(assignedRooms, revenue, inventory);
            }
            cells[index] = cell;
        }
        return cells;
    }

    // Static Web Apps abandons a linked-backend call at ~45s. Without a client
    // deadline a stalled request never settles and the page waits forever.
    const REQUEST_TIMEOUT_MS = 40000;

    async function fetchJson(url) {
        const settings = { headers: { Accept: "application/json" } };
        if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) {
            settings.signal = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
        }

        let response;
        try {
            response = await fetch(url, settings);
        } catch (networkError) {
            const error = new Error(
                networkError.name === "TimeoutError" || networkError.name === "AbortError"
                    ? `The request took longer than ${REQUEST_TIMEOUT_MS / 1000} seconds and was cancelled. Try a narrower date range.`
                    : `Could not reach the Supplement API: ${networkError.message}`
            );
            error.status = 0;
            throw error;
        }

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
        sumRowCells,
        fetchMetadata,
        fetchGrid,
        fetchDetail
    };
}));
