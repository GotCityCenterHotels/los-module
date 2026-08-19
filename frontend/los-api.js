(function initializeLosApi(root) {
    "use strict";

    const HOTEL_CACHE_TTL_MS = 5 * 60 * 1000;
    const hotelRequests = new Map();
    // Static Web Apps gives up on a linked backend at ~45s. Without a client
    // deadline a stalled connection never settles at all, leaving buttons
    // disabled and the page stuck until a manual reload.
    const REQUEST_TIMEOUT_MS = 40000;
    // Selecting scattered months used to cost one round trip after another, so
    // a four-month selection waited four times as long as a one-month one. They
    // are independent queries against a published read model, so they overlap.
    // The cap is deliberate: the Functions app holds a small connection pool,
    // and firing a dozen at once would just queue them there instead.
    const MAX_CONCURRENT_RANGE_REQUESTS = 3;

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
            // The underlying fetch failure (DNS, TLS, CORS, offline) is the only
            // diagnostic there is on this path, and the message shown to the user
            // deliberately does not carry it, so log it before it is discarded.
            console.error("Network request failed", error);
            throw new Error("Could not reach the server. Check your connection and try again.");
        }

        // The success path is the big one - a year of facts is megabytes of
        // JSON - and reading it as text first materialises the whole body as a
        // string before the parser ever sees it. response.json() parses the
        // bytes directly. The text path below still handles everything that is
        // not a healthy JSON response, where the raw body is the diagnostic.
        const contentType = response.headers?.get?.("content-type") || "";
        if (response.ok && /\bjson\b/i.test(contentType) && typeof response.json === "function") {
            try {
                return await response.json();
            }
            catch (error) {
                console.error(`Malformed JSON body from ${url} (HTTP ${response.status})`, error);
                throw new Error(`API returned a malformed JSON body (HTTP ${response.status}).`);
            }
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

    // Every range this returns covers exactly the selected months: the runs are
    // contiguous, so every month between a run's first and last is itself
    // selected. That is what lets the callers drop their own post-fetch month
    // filter, which they have to now - a server-rolled row carries its period
    // start, and a week bucket can legitimately start in the month before the
    // one that was asked for.
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

    // Kept for callers that combine collections which can genuinely overlap.
    // The range path below no longer needs it: buildContiguousMonthRanges emits
    // disjoint date ranges, so no key can appear in two collections and the
    // merge was a per-row key allocation that could never combine anything.
    function mergeFactRows(factCollections) {
        const merged = new Map();

        for (const facts of factCollections) {
            for (const fact of facts || []) {
                const key = JSON.stringify([
                    fact.arrivalDate,
                    fact.hotelName,
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
                        hotelName: fact.hotelName,
                        enterpriseId: fact.enterpriseId,
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

    async function mapWithConcurrency(items, limit, run) {
        const results = new Array(items.length);
        let nextIndex = 0;

        async function worker() {
            for (;;) {
                const index = nextIndex;
                nextIndex += 1;
                if (index >= items.length) return;
                results[index] = await run(items[index], index);
            }
        }

        await Promise.all(
            Array.from({ length: Math.min(limit, items.length) }, worker)
        );
        return results;
    }

    async function fetchLosFactRanges({
        apiBaseUrl = "/api",
        startDate,
        endDate,
        lyComparisonBasis,
        grain = "day",
        selectedMonths = [],
        fetcher = fetchJson
    }) {
        const ranges = buildContiguousMonthRanges(selectedMonths, startDate, endDate);
        const collections = await mapWithConcurrency(
            ranges,
            MAX_CONCURRENT_RANGE_REQUESTS,
            async (range) => {
                // The grain goes to the server, which rolls the date dimension
                // up in SQL. A year at day grain is ~170k rows the browser only
                // ever reduces to a few hundred; at month grain the server sends
                // the few hundred. LosData still aggregates what arrives - on
                // rolled-up rows that is an identity transform, because
                // date_trunc lands them on the same period keys getPeriodKey
                // computes.
                const params = new URLSearchParams({
                    startDate: range.startDate,
                    endDate: range.endDate,
                    lyComparisonBasis,
                    grain
                });
                try {
                    const payload = await fetcher(`${apiBaseUrl}/los/facts?${params}`);
                    return payload.data || [];
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
        );

        // Disjoint ranges, so concatenation is exact - and for the single-range
        // case, which is every request that does not use the month picker, it is
        // no work at all rather than a full re-keying of every row.
        const data = collections.length === 1 ? collections[0] : collections.flat();
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
        MAX_CONCURRENT_RANGE_REQUESTS,
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
