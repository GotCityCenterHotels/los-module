(function initializeLosApi(root) {
    "use strict";

    const HOTEL_CACHE_TTL_MS = 5 * 60 * 1000;
    const hotelRequests = new Map();
    // Static Web Apps gives up on a linked backend at ~45s. Without a client
    // deadline a stalled connection never settles at all, leaving buttons
    // disabled and the page stuck until a manual reload.
    const REQUEST_TIMEOUT_MS = 40000;

    async function fetchJson(url, options) {
        const settings = {...options};
        if (!settings.signal && typeof AbortSignal !== "undefined" && AbortSignal.timeout) {
            settings.signal = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
        }

        let response;
        try {
            response = await fetch(url, settings);
        }
        catch (error) {
            if (error.name === "TimeoutError" || error.name === "AbortError") {
                throw new Error(
                    `The request took longer than ${REQUEST_TIMEOUT_MS / 1000} seconds and was cancelled. `
                    + "Try a narrower date range."
                );
            }
            throw new Error(`Could not reach the API: ${error.message}`);
        }

        const raw = await response.text();
        let payload;

        try {
            payload = raw ? JSON.parse(raw) : {};
        }
        catch {
            // A short plain-text body ("Function host is not running.") is a
            // useful diagnostic. An HTML body is a gateway error page - dumping
            // its markup into the UI helps nobody, so log it and show something
            // the user can act on instead.
            console.error(`Non-JSON response from ${url} (HTTP ${response.status}):`, raw.slice(0, 2000));
            const preview = raw.replace(/\s+/g, " ").trim();
            const looksLikeMarkup = /^\s*[<{]/.test(raw) || preview.length > 200;

            if (response.status === 504 || response.status === 502) {
                throw new Error("The server took too long to respond. Try a narrower date range.");
            }
            throw new Error(
                looksLikeMarkup || !preview
                    ? `API returned HTTP ${response.status}.`
                    : `API returned HTTP ${response.status}: ${preview}`
            );
        }

        if (!response.ok) {
            throw new Error(payload.error || `API returned HTTP ${response.status}`);
        }

        return payload;
    }

    function getLastDayOfMonth(monthKey) {
        const [year, month] = monthKey.split("-").map(Number);
        return new Date(Date.UTC(year, month, 0)).getUTCDate();
    }

    function buildContiguousMonthRanges(selectedMonths, startDate, endDate) {
        if (!selectedMonths || selectedMonths.length === 0) {
            return [{ startDate, endDate }];
        }

        const months = Array.from(new Set(selectedMonths)).sort();
        const ranges = [];
        let rangeStart = months[0];
        let rangeEnd = months[0];

        function monthIndex(monthKey) {
            const match = /^(\d{4})-(\d{2})$/.exec(monthKey);
            if (!match || Number(match[2]) < 1 || Number(match[2]) > 12) {
                throw new Error(`Invalid selected month: ${monthKey}`);
            }
            return Number(match[1]) * 12 + Number(match[2]) - 1;
        }

        monthIndex(rangeStart);
        for (const month of months.slice(1)) {
            if (monthIndex(month) === monthIndex(rangeEnd) + 1) {
                rangeEnd = month;
                continue;
            }
            ranges.push(toDateRange(rangeStart, rangeEnd));
            rangeStart = month;
            rangeEnd = month;
        }
        ranges.push(toDateRange(rangeStart, rangeEnd));
        return ranges;
    }

    function toDateRange(firstMonth, lastMonth) {
        return {
            startDate: `${firstMonth}-01`,
            endDate: `${lastMonth}-${String(getLastDayOfMonth(lastMonth)).padStart(2, "0")}`
        };
    }

    function mergeFactRows(factCollections) {
        const merged = new Map();

        for (const facts of factCollections) {
            for (const fact of facts || []) {
                const key = JSON.stringify([
                    fact.arrivalDate,
                    fact.hotelCode,
                    fact.scenario,
                    Number(fact.los)
                ]);
                const existing = merged.get(key);
                if (existing) {
                    existing.bookingCount += Number(fact.bookingCount) || 0;
                    existing.nightCount += Number(fact.nightCount) || 0;
                }
                else {
                    merged.set(key, {
                        arrivalDate: fact.arrivalDate,
                        hotelCode: fact.hotelCode,
                        scenario: fact.scenario,
                        los: Number(fact.los),
                        bookingCount: Number(fact.bookingCount) || 0,
                        nightCount: Number(fact.nightCount) || 0
                    });
                }
            }
        }

        return Array.from(merged.values());
    }

    async function fetchLosFactRanges({
        apiBaseUrl = "/api",
        startDate,
        endDate,
        lyComparisonBasis,
        selectedMonths = [],
        fetcher = fetchJson
    }) {
        const ranges = buildContiguousMonthRanges(selectedMonths, startDate, endDate);
        const collections = [];

        for (const range of ranges) {
            const params = new URLSearchParams({
                startDate: range.startDate,
                endDate: range.endDate,
                lyComparisonBasis
            });
            try {
                const payload = await fetcher(`${apiBaseUrl}/los/facts?${params}`);
                collections.push(payload.data || []);
            }
            catch (error) {
                const rangeError = new Error(
                    `LOS facts failed for ${range.startDate} to ${range.endDate}: ${error.message}`
                );
                rangeError.cause = error;
                rangeError.range = range;
                throw rangeError;
            }
        }

        const data = mergeFactRows(collections);
        return { data, rowCount: data.length, ranges };
    }

    function hotelCacheKey({ apiBaseUrl, startDate, endDate, lyComparisonBasis }) {
        return `los-hotels:${apiBaseUrl}:${startDate}:${endDate}:${lyComparisonBasis}`;
    }

    function availableSessionStorage() {
        try {
            return root.sessionStorage || null;
        }
        catch {
            return null;
        }
    }

    async function fetchHotelList({
        apiBaseUrl = "/api",
        startDate,
        endDate,
        lyComparisonBasis,
        forceRefresh = false,
        fetcher = fetchJson,
        storage = availableSessionStorage(),
        now = Date.now()
    }) {
        const request = { apiBaseUrl, startDate, endDate, lyComparisonBasis };
        const key = hotelCacheKey(request);

        if (!forceRefresh && storage) {
            try {
                const cached = JSON.parse(storage.getItem(key));
                if (cached && now - cached.savedAt < HOTEL_CACHE_TTL_MS && Array.isArray(cached.data)) {
                    return { data: cached.data, fromCache: true };
                }
            }
            catch {
                // Ignore unavailable, malformed, or quota-constrained storage.
            }
        }

        if (hotelRequests.has(key)) {
            return hotelRequests.get(key);
        }

        const params = new URLSearchParams({ startDate, endDate, lyComparisonBasis });
        const pending = fetcher(`${apiBaseUrl}/los/hotels?${params}`)
            .then((payload) => {
                const result = { data: payload.data || [], fromCache: false };
                if (storage) {
                    try {
                        storage.setItem(key, JSON.stringify({ savedAt: Date.now(), data: result.data }));
                    }
                    catch {
                        // Hotel metadata remains usable when browser storage is unavailable.
                    }
                }
                return result;
            })
            .finally(() => hotelRequests.delete(key));

        hotelRequests.set(key, pending);
        return pending;
    }

    const api = {
        HOTEL_CACHE_TTL_MS,
        fetchJson,
        buildContiguousMonthRanges,
        mergeFactRows,
        fetchLosFactRanges,
        fetchHotelList
    };

    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }

    root.LosApi = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
