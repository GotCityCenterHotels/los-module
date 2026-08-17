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

    // localeCompare rebuilds its collator from the arguments on every call, and
    // these comparators run tens of thousands of times per render. No locale
    // argument, so the ordering is identical to the localeCompare() calls this
    // replaced - including for Å/Ä/Ö in hotel names.
    const collator = new Intl.Collator();

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

    // Facts repeat each arrival date once per hotel, scenario, and LOS, so a
    // year of rows carries only ~365 distinct dates. Resolving each one once
    // turns a regex - and, for weeks, a Date round trip - per row into a lookup,
    // and hands back the same string instance every time, which is what makes
    // it a cheap Map key below.
    function createPeriodKeyResolver(grain) {
        const resolved = new Map();
        return (arrivalDate) => {
            let periodKey = resolved.get(arrivalDate);
            if (periodKey === undefined) {
                periodKey = getPeriodKey(arrivalDate, grain);
                resolved.set(arrivalDate, periodKey);
            }
            return periodKey;
        };
    }

    function getLosBucket(los, buckets = DEFAULT_LOS_BUCKETS) {
        const numericLos = Number(los);
        const bucket = buckets.find(({ min, max }) =>
            numericLos >= Number(min) && numericLos <= Number(max)
        );
        return bucket ? bucket.label : null;
    }

    // Same idea as the period keys: LOS is a small set of integers, so the
    // linear bucket scan runs once per distinct value instead of once per fact.
    function createBucketIndexResolver(buckets) {
        const resolved = new Map();
        return (los) => {
            const numericLos = Number(los);
            let index = resolved.get(numericLos);
            if (index === undefined) {
                index = buckets.findIndex(({ min, max }) =>
                    numericLos >= Number(min) && numericLos <= Number(max)
                );
                resolved.set(numericLos, index);
            }
            return index;
        };
    }

    // Grouping used to build one composite key string per fact per pass. Those
    // strings are freshly allocated, so the engine had to hash every character
    // of every one of them on both the lookup and the insert - which measured
    // as the single most expensive thing in the render path, well ahead of
    // parsing the response. Nesting one Map level per field instead reuses the
    // string instances the facts already carry, whose hashes are computed once
    // and then cached, and it is roughly thirteen times faster on a year of
    // data. Everything that groups facts goes through this.
    function childMap(parent, key) {
        let child = parent.get(key);
        if (child === undefined) {
            child = new Map();
            parent.set(key, child);
        }
        return child;
    }

    function filterFacts(facts, { hotelCodes = null, scenario = null } = {}) {
        const includeFact = createFactPredicate({ hotelCodes, scenario });
        return (Array.isArray(facts) ? facts : Array.from(facts)).filter(includeFact);
    }

    function createFactPredicate({ hotelCodes = null, scenario = null, selectedMonths = [] } = {}) {
        const hotels = hotelCodes === null
            ? null
            : new Set(Array.isArray(hotelCodes) ? hotelCodes : [hotelCodes]);
        const scenarios = scenario === null || scenario === "all"
            ? null
            : new Set(Array.isArray(scenario) ? scenario : [scenario]);
        const months = selectedMonths?.length ? new Set(selectedMonths) : null;

        // "Every hotel, every scenario, every month" is the common case, and
        // recognising it once beats re-testing three inactive filters for each
        // of ~170k facts.
        if (hotels === null && scenarios === null && months === null) {
            return () => true;
        }

        return (fact) =>
            (hotels === null || hotels.has(fact.hotelCode))
            && (scenarios === null || scenarios.has(fact.scenario))
            && (months === null || months.has(String(fact.arrivalDate).slice(0, 7)));
    }

    function filterByMonths(facts, selectedMonths = []) {
        if (!selectedMonths || selectedMonths.length === 0) {
            return facts;
        }

        const months = new Set(selectedMonths);
        return facts.filter((fact) => months.has(String(fact.arrivalDate).slice(0, 7)));
    }

    function aggregateFacts(
        facts,
        { grain = "day", hotelCodes = null, scenario = null, portfolio = false } = {}
    ) {
        const includeFact = createFactPredicate({ hotelCodes, scenario });
        const periodKeyFor = createPeriodKeyResolver(grain);
        const index = new Map();
        const rows = [];

        for (const fact of facts) {
            if (!includeFact(fact)) continue;
            const periodKey = periodKeyFor(fact.arrivalDate);
            const hotelCode = portfolio ? "Total" : fact.hotelCode;
            const los = Number(fact.los);
            const byLos = childMap(
                childMap(childMap(index, periodKey), hotelCode),
                fact.scenario
            );
            let group = byLos.get(los);

            if (group === undefined) {
                group = {
                    periodKey,
                    hotelCode,
                    scenario: fact.scenario,
                    los,
                    bookingCount: 0,
                    nightCount: 0
                };
                byLos.set(los, group);
                rows.push(group);
            }

            group.bookingCount += Number(fact.bookingCount) || 0;
            group.nightCount += Number(fact.nightCount) || 0;
        }

        return sortRows(rows);
    }

    function calculateAverageLos(facts, options = {}) {
        const exactLosFacts = aggregateFacts(facts, options);
        const index = new Map();
        const rows = [];

        for (const fact of exactLosFacts) {
            const byScenario = childMap(childMap(index, fact.periodKey), fact.hotelCode);
            let group = byScenario.get(fact.scenario);

            if (group === undefined) {
                group = {
                    periodKey: fact.periodKey,
                    hotelCode: fact.hotelCode,
                    scenario: fact.scenario,
                    bookingCount: 0,
                    nightCount: 0,
                    averageLos: null
                };
                byScenario.set(fact.scenario, group);
                rows.push(group);
            }

            group.bookingCount += fact.bookingCount;
            group.nightCount += fact.nightCount;
        }

        return sortRows(finalizeAverages(rows));
    }

    function finalizeAverages(rows) {
        for (const group of rows) {
            group.averageLos = group.bookingCount > 0
                ? group.nightCount / group.bookingCount
                : null;
        }
        return rows;
    }

    function calculateAverageView(
        facts,
        { grain = "day", hotelCodes = null, scenario = null, selectedMonths = [] } = {}
    ) {
        const includeFact = createFactPredicate({ hotelCodes, scenario, selectedMonths });
        const periodKeyFor = createPeriodKeyResolver(grain);
        const hotelIndex = new Map();
        const portfolioIndex = new Map();
        const summaryIndex = new Map();
        const hotelRows = [];
        const portfolioRows = [];
        const summaryRows = [];

        // One pass, three accumulations. Written out rather than routed through
        // a shared helper because this body runs once per fact and the closure
        // call plus the seed object the helper had to allocate for every fact
        // were both showing up in the render cost.
        for (const fact of facts) {
            if (!includeFact(fact)) continue;
            const periodKey = periodKeyFor(fact.arrivalDate);
            const scenarioName = fact.scenario;
            const bookingCount = Number(fact.bookingCount) || 0;
            const nightCount = Number(fact.nightCount) || 0;

            const hotelScenarios = childMap(
                childMap(hotelIndex, periodKey), fact.hotelCode
            );
            let hotelGroup = hotelScenarios.get(scenarioName);
            if (hotelGroup === undefined) {
                hotelGroup = {
                    periodKey,
                    hotelCode: fact.hotelCode,
                    scenario: scenarioName,
                    bookingCount: 0,
                    nightCount: 0,
                    averageLos: null
                };
                hotelScenarios.set(scenarioName, hotelGroup);
                hotelRows.push(hotelGroup);
            }
            hotelGroup.bookingCount += bookingCount;
            hotelGroup.nightCount += nightCount;

            const portfolioScenarios = childMap(portfolioIndex, periodKey);
            let portfolioGroup = portfolioScenarios.get(scenarioName);
            if (portfolioGroup === undefined) {
                portfolioGroup = {
                    periodKey,
                    hotelCode: "Total",
                    scenario: scenarioName,
                    bookingCount: 0,
                    nightCount: 0,
                    averageLos: null
                };
                portfolioScenarios.set(scenarioName, portfolioGroup);
                portfolioRows.push(portfolioGroup);
            }
            portfolioGroup.bookingCount += bookingCount;
            portfolioGroup.nightCount += nightCount;

            let summaryGroup = summaryIndex.get(scenarioName);
            if (summaryGroup === undefined) {
                summaryGroup = {
                    periodKey: "All",
                    hotelCode: "Total",
                    scenario: scenarioName,
                    bookingCount: 0,
                    nightCount: 0,
                    averageLos: null
                };
                summaryIndex.set(scenarioName, summaryGroup);
                summaryRows.push(summaryGroup);
            }
            summaryGroup.bookingCount += bookingCount;
            summaryGroup.nightCount += nightCount;
        }

        const hotels = sortRows(finalizeAverages(hotelRows));
        const portfolio = sortRows(finalizeAverages(portfolioRows));
        const summary = sortRows(finalizeAverages(summaryRows));
        return {
            hotelRows: hotels,
            portfolioRows: portfolio,
            summaryRows: summary,
            rows: sortRows([...hotels, ...portfolio])
        };
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
            buckets = DEFAULT_LOS_BUCKETS,
            selectedMonths = []
        } = {}
    ) {
        if (metric !== "bookings" && metric !== "nights") {
            throw new Error(`Unsupported distribution metric: ${metric}`);
        }

        const includeFact = createFactPredicate({
            hotelCodes,
            scenario,
            selectedMonths
        });
        const periodKeyFor = createPeriodKeyResolver(grain);
        const bucketIndexFor = createBucketIndexResolver(buckets);
        const useNights = metric === "nights";
        const index = new Map();
        const groups = [];

        for (const fact of facts) {
            if (!includeFact(fact)) continue;
            const periodKey = periodKeyFor(fact.arrivalDate);
            const hotelCode = portfolio ? "Total" : fact.hotelCode;
            const byScenario = childMap(childMap(index, periodKey), hotelCode);
            let group = byScenario.get(fact.scenario);

            if (group === undefined) {
                group = {
                    periodKey,
                    hotelCode,
                    scenario: fact.scenario,
                    metric,
                    // One slot per bucket, positional. The previous shape built
                    // a fresh Map for every period and hotel, then looked each
                    // bucket up by label once per fact.
                    bucketValues: new Array(buckets.length).fill(0)
                };
                byScenario.set(fact.scenario, group);
                groups.push(group);
            }

            const bucketIndex = bucketIndexFor(fact.los);
            if (bucketIndex !== -1) {
                group.bucketValues[bucketIndex] +=
                    Number(useNights ? fact.nightCount : fact.bookingCount) || 0;
            }
        }

        return sortRows(groups.map((group) => {
            const rawValues = group.bucketValues;
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
        }));
    }

    // A day-grain year across a full portfolio is ~12k rows, and collating a
    // hotel name is by far the most expensive thing a comparator can do. There
    // are only ever a handful of distinct names, so they are ordered once and
    // the comparator comes down to integer subtraction.
    function sortRows(rows) {
        if (rows.length < 2) return rows;

        const hotelRank = new Map();
        for (const row of rows) hotelRank.set(row.hotelCode, 0);
        Array.from(hotelRank.keys())
            .sort((left, right) => collator.compare(String(left), String(right)))
            .forEach((hotelCode, index) => hotelRank.set(hotelCode, index));

        return rows.sort((left, right) => {
            // Period keys are fixed-shape ISO strings, or the literal "All" on
            // rows that are all "All", so a plain comparison orders them exactly
            // as the collator did.
            if (left.periodKey !== right.periodKey) {
                return left.periodKey < right.periodKey ? -1 : 1;
            }
            const rankDifference = hotelRank.get(left.hotelCode) - hotelRank.get(right.hotelCode);
            if (rankDifference !== 0) return rankDifference;
            return ((SCENARIO_ORDER[left.scenario] || 99) - (SCENARIO_ORDER[right.scenario] || 99))
                || (Number(left.los || 0) - Number(right.los || 0));
        });
    }

    return {
        DEFAULT_LOS_BUCKETS,
        getPeriodKey,
        getLosBucket,
        filterFacts,
        createFactPredicate,
        filterByMonths,
        aggregateFacts,
        calculateAverageLos,
        calculateAverageView,
        calculatePercentages,
        calculateDistribution
    };
}));
