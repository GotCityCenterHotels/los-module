(function initializeLosData(root, factory) {
    const api = factory();

    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }

    root.LosData = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createLosData() {
    "use strict";

    const DEFAULT_LOS_BUCKETS = Object.freeze([
        Object.freeze({ label: "1", min: 1, max: 1 }),
        Object.freeze({ label: "2", min: 2, max: 2 }),
        Object.freeze({ label: "3", min: 3, max: 3 }),
        Object.freeze({ label: "4", min: 4, max: 4 }),
        Object.freeze({ label: "5+", min: 5, max: Infinity })
    ]);

    const SCENARIO_ORDER = { current: 1, ly: 2, spit: 3 };

    function getPeriodKey(arrivalDate, grain = "day") {
        const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(arrivalDate));
        if (!match) {
            throw new Error(`Invalid ISO arrival date: ${arrivalDate}`);
        }

        const [, year, month, day] = match;

        if (grain === "all") {
            return "All";
        }
        if (grain === "year") {
            return `${year}-01-01`;
        }
        if (grain === "month") {
            return `${year}-${month}-01`;
        }
        if (grain === "day") {
            return `${year}-${month}-${day}`;
        }
        if (grain === "week") {
            const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
            const daysSinceMonday = (date.getUTCDay() + 6) % 7;
            date.setUTCDate(date.getUTCDate() - daysSinceMonday);
            return date.toISOString().slice(0, 10);
        }

        throw new Error(`Unsupported grain: ${grain}`);
    }

    function getLosBucket(los, buckets = DEFAULT_LOS_BUCKETS) {
        const numericLos = Number(los);
        const bucket = buckets.find(({ min, max }) =>
            numericLos >= Number(min) && numericLos <= Number(max)
        );
        return bucket ? bucket.label : null;
    }

    function filterFacts(facts, { hotelCodes = null, scenario = null } = {}) {
        const hotels = hotelCodes === null
            ? null
            : new Set(Array.isArray(hotelCodes) ? hotelCodes : [hotelCodes]);
        const scenarios = scenario === null || scenario === "all"
            ? null
            : new Set(Array.isArray(scenario) ? scenario : [scenario]);

        return facts.filter((fact) =>
            (hotels === null || hotels.has(fact.hotelCode))
            && (scenarios === null || scenarios.has(fact.scenario))
        );
    }

    function aggregateFacts(
        facts,
        { grain = "day", hotelCodes = null, scenario = null, portfolio = false } = {}
    ) {
        const groups = new Map();

        for (const fact of filterFacts(facts, { hotelCodes, scenario })) {
            const periodKey = getPeriodKey(fact.arrivalDate, grain);
            const hotelCode = portfolio ? "Total" : fact.hotelCode;
            const los = Number(fact.los);
            const key = JSON.stringify([periodKey, hotelCode, fact.scenario, los]);
            const group = groups.get(key) || {
                periodKey,
                hotelCode,
                scenario: fact.scenario,
                los,
                bookingCount: 0,
                nightCount: 0
            };

            group.bookingCount += Number(fact.bookingCount) || 0;
            group.nightCount += Number(fact.nightCount) || 0;
            groups.set(key, group);
        }

        return Array.from(groups.values()).sort(compareRows);
    }

    function calculateAverageLos(facts, options = {}) {
        const exactLosFacts = aggregateFacts(facts, options);
        const groups = new Map();

        for (const fact of exactLosFacts) {
            const key = JSON.stringify([fact.periodKey, fact.hotelCode, fact.scenario]);
            const group = groups.get(key) || {
                periodKey: fact.periodKey,
                hotelCode: fact.hotelCode,
                scenario: fact.scenario,
                bookingCount: 0,
                nightCount: 0,
                averageLos: null
            };

            group.bookingCount += fact.bookingCount;
            group.nightCount += fact.nightCount;
            groups.set(key, group);
        }

        for (const group of groups.values()) {
            group.averageLos = group.bookingCount > 0
                ? group.nightCount / group.bookingCount
                : null;
        }

        return Array.from(groups.values()).sort(compareRows);
    }

    function calculatePercentages(values) {
        const total = values.reduce((sum, value) => sum + Number(value || 0), 0);
        return values.map((value) => total > 0 ? Number(value || 0) / total * 100 : 0);
    }

    function calculateDistribution(
        facts,
        {
            grain = "day",
            hotelCodes = null,
            scenario = null,
            portfolio = false,
            metric = "bookings",
            buckets = DEFAULT_LOS_BUCKETS
        } = {}
    ) {
        if (!new Set(["bookings", "nights"]).has(metric)) {
            throw new Error(`Unsupported distribution metric: ${metric}`);
        }

        const exactLosFacts = aggregateFacts(
            facts,
            { grain, hotelCodes, scenario, portfolio }
        );
        const groups = new Map();

        for (const fact of exactLosFacts) {
            const key = JSON.stringify([fact.periodKey, fact.hotelCode, fact.scenario]);
            const group = groups.get(key) || {
                periodKey: fact.periodKey,
                hotelCode: fact.hotelCode,
                scenario: fact.scenario,
                metric,
                total: 0,
                bucketValues: new Map(buckets.map(({ label }) => [label, 0]))
            };
            const bucketLabel = getLosBucket(fact.los, buckets);

            if (bucketLabel !== null) {
                const value = metric === "nights" ? fact.nightCount : fact.bookingCount;
                group.bucketValues.set(
                    bucketLabel,
                    group.bucketValues.get(bucketLabel) + value
                );
            }
            groups.set(key, group);
        }

        return Array.from(groups.values()).map((group) => {
            const rawValues = buckets.map(({ label }) => group.bucketValues.get(label));
            const percentages = calculatePercentages(rawValues);
            const values = buckets.map(({ label }, index) => ({
                label,
                value: rawValues[index],
                percentage: percentages[index]
            }));

            return {
                periodKey: group.periodKey,
                hotelCode: group.hotelCode,
                scenario: group.scenario,
                metric: group.metric,
                total: rawValues.reduce((sum, value) => sum + value, 0),
                values
            };
        }).sort(compareRows);
    }

    function compareRows(left, right) {
        return String(left.periodKey).localeCompare(String(right.periodKey))
            || String(left.hotelCode).localeCompare(String(right.hotelCode))
            || (SCENARIO_ORDER[left.scenario] || 99) - (SCENARIO_ORDER[right.scenario] || 99)
            || Number(left.los || 0) - Number(right.los || 0);
    }

    return {
        DEFAULT_LOS_BUCKETS,
        getPeriodKey,
        getLosBucket,
        filterFacts,
        aggregateFacts,
        calculateAverageLos,
        calculatePercentages,
        calculateDistribution
    };
}));
