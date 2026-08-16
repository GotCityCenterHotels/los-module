(function initializeCostData(root) {
    "use strict";

    const Format = root.LosFormat
        || (typeof require === "function" ? require("./los-format.js") : null);
    if (!Format) {
        throw new Error(
            "los-format.js did not load - hard refresh the page, and check that "
            + "the script deployed alongside costdata-data.js."
        );
    }

    const DATASET_RULES = {
        roomRevenue: {
            dimensions: ["amountCurrency"],
            values: [
                "roomRevenueExclProducts1Net",
                "productRevenue1Net",
                "roomRevenueInclProducts1Net"
            ]
        },
        payments: {
            dimensions: ["amountCurrency"],
            values: ["totalPaymentAmountGrossValue"]
        },
        breakfast: {
            dimensions: [],
            values: ["breakfastTotal", "breakfastNetCost"]
        },
        parking: {
            dimensions: ["service"],
            values: [
                "totalReservationsUsingParking",
                "totalParkingSpots",
                "totalParkingAmountNetValue"
            ]
        },
        arrivalsDepartures: {
            dimensions: [],
            values: ["totalArrivals", "totalDepartures"]
        }
    };

    // The GOP statement, in the order it is presented. Nothing else decides row
    // order: the view renders exactly this list.
    const GOP_LINES = Object.freeze([
        Object.freeze({key: "roomRevenue", label: "Room revenue incl products", type: "revenue"}),
        Object.freeze({key: "parkingRevenue", label: "Parking revenue", type: "revenue"}),
        Object.freeze({key: "breakfastCost", label: "Breakfast cost", type: "cost"}),
        Object.freeze({key: "distributionCost", label: "Distribution cost", type: "cost"}),
        Object.freeze({key: "franchiseCardCost", label: "Franchise & card cost", type: "cost"}),
        Object.freeze({key: "cleaningCost", label: "Cleaning cost", type: "cost"}),
        Object.freeze({key: "arrivalCost", label: "Arrival cost", type: "cost"}),
        Object.freeze({key: "gop", label: "Gross Operating Profit (GOP)", type: "result"})
    ]);

    const REVENUE_KEYS = GOP_LINES.filter(({type}) => type === "revenue").map(({key}) => key);
    const COST_KEYS = GOP_LINES.filter(({type}) => type === "cost").map(({key}) => key);

    function periodKey(dateValue, grain) {
        if (grain === "year") return `${dateValue.slice(0, 4)}-01-01`;
        if (grain === "month") return `${dateValue.slice(0, 7)}-01`;
        if (grain === "week") {
            // ISO weeks start on Monday; the bucket key is that Monday, which
            // matches how los-data.js buckets LOS facts.
            const [year, month, day] = dateValue.slice(0, 10).split("-").map(Number);
            const date = new Date(Date.UTC(year, month - 1, day));
            const daysSinceMonday = (date.getUTCDay() + 6) % 7;
            date.setUTCDate(date.getUTCDate() - daysSinceMonday);
            return date.toISOString().slice(0, 10);
        }
        return dateValue;
    }

    function filterRows(rows, hotelName) {
        return (rows || []).filter((row) => !hotelName || row.hotelName === hotelName);
    }

    function aggregate(dataset, rows, { grain = "day", hotelName = "" } = {}) {
        const rule = DATASET_RULES[dataset];
        if (!rule) throw new Error(`Unknown cost dataset: ${dataset}`);

        const grouped = new Map();
        for (const row of filterRows(rows, hotelName)) {
            const stayDate = periodKey(row.stayDate, grain);
            const keyParts = [stayDate, row.hotelName || "Unspecified"];
            for (const dimension of rule.dimensions) keyParts.push(row[dimension] || "Unspecified");
            const key = JSON.stringify(keyParts);
            let result = grouped.get(key);

            if (!result) {
                result = {
                    stayDate,
                    hotelName: row.hotelName || "Unspecified",
                    lastUpdatedAt: row.lastUpdatedAt || null
                };
                for (const dimension of rule.dimensions) {
                    result[dimension] = row[dimension] || "Unspecified";
                }
                for (const value of rule.values) result[value] = 0;
                grouped.set(key, result);
            }

            for (const value of rule.values) result[value] += Number(row[value]) || 0;
            if (row.lastUpdatedAt && (!result.lastUpdatedAt || row.lastUpdatedAt > result.lastUpdatedAt)) {
                result.lastUpdatedAt = row.lastUpdatedAt;
            }
        }

        return Array.from(grouped.values()).sort((left, right) =>
            left.stayDate.localeCompare(right.stayDate)
            || left.hotelName.localeCompare(right.hotelName)
            || JSON.stringify(left).localeCompare(JSON.stringify(right))
        );
    }

    function sum(rows, field) {
        return rows.reduce((total, row) => total + (Number(row[field]) || 0), 0);
    }

    function sumByCurrency(rows, field) {
        const totals = {};
        for (const row of rows) {
            const currency = row.amountCurrency || "Unspecified";
            totals[currency] = (totals[currency] || 0) + (Number(row[field]) || 0);
        }
        return totals;
    }

    function summarize(data, { hotelName = "" } = {}) {
        const roomRevenue = filterRows(data.roomRevenue, hotelName);
        const payments = filterRows(data.payments, hotelName);
        const breakfast = filterRows(data.breakfast, hotelName);
        const parking = filterRows(data.parking, hotelName);
        const movements = filterRows(data.arrivalsDepartures, hotelName);

        return {
            roomRevenue: sumByCurrency(roomRevenue, "roomRevenueInclProducts1Net"),
            payments: sumByCurrency(payments, "totalPaymentAmountGrossValue"),
            breakfastCost: sum(breakfast, "breakfastNetCost"),
            parkingNet: sum(parking, "totalParkingAmountNetValue"),
            arrivals: sum(movements, "totalArrivals"),
            departures: sum(movements, "totalDepartures")
        };
    }

    // -----------------------------------------------------------------------
    // Gross Operating Profit
    //
    // Every cost below is derived from the property's saved Cost Input
    // configuration - nothing is hardcoded. Where a figure cannot be derived,
    // the calculation records a flag instead of substituting a default, so the
    // view can say which number is missing and why.
    // -----------------------------------------------------------------------

    function numberOf(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function percentOf(amount, percent) {
        return amount * numberOf(percent) / 100;
    }

    // A checkbox that has never been saved arrives as undefined, a saved one as
    // a real boolean, and a form-encoded one as a string. "false" is a
    // non-empty string and therefore truthy, which is exactly the bug this
    // avoids.
    function isEnabled(value, fallback = true) {
        if (value === undefined || value === null || value === "") return fallback;
        if (typeof value === "boolean") return value;
        return !["false", "0", "no", "off"].includes(String(value).trim().toLowerCase());
    }

    // The tier whose [min, max] band contains count. An empty max is an
    // open-ended top tier.
    //
    // A count outside every band falls back to the nearest configured band -
    // the highest whose minimum it clears, or the lowest when it clears none -
    // rather than contributing no staffing at all. A property that configures
    // thresholds up to 200 guests and then serves 240 has not said "no staff
    // above 200"; it has said "this is the top band", and costing that day at
    // zero hours understated the cost and produced a warning nobody could act
    // on. Returns null only when there are no tiers at all, which really is a
    // configuration gap.
    function matchTier(tiers, count, minKey, maxKey) {
        const rows = tiers || [];
        if (!rows.length) return null;
        let below = null;
        let lowest = null;
        for (const tier of rows) {
            const minimum = numberOf(tier[minKey]);
            const rawMaximum = tier[maxKey];
            const maximum = rawMaximum === null || rawMaximum === undefined || rawMaximum === ""
                ? Infinity
                : numberOf(rawMaximum);
            if (count >= minimum && count <= maximum) return tier;
            if (minimum <= count && (below === null || minimum > numberOf(below[minKey]))) {
                below = tier;
            }
            if (lowest === null || minimum < numberOf(lowest[minKey])) lowest = tier;
        }
        return below || lowest;
    }

    function dailyTotals(rows, field) {
        const totals = new Map();
        for (const row of rows) {
            const day = row.stayDate;
            totals.set(day, (totals.get(day) || 0) + numberOf(row[field]));
        }
        return totals;
    }

    function hotelsInScope(data, hotelName) {
        const names = new Set();
        for (const rows of Object.values(data || {})) {
            for (const row of rows || []) {
                if (row && row.hotelName) names.add(row.hotelName);
            }
        }
        if (hotelName) return names.has(hotelName) ? [hotelName] : [];
        return Array.from(names).sort((left, right) => left.localeCompare(right));
    }

    // Only the property's own currency is summed. Rows in another currency are
    // reported rather than silently converted or silently added.
    function currencyTotal(rows, field, currency, excluded) {
        let total = 0;
        for (const row of rows) {
            const rowCurrency = row.amountCurrency || "Unspecified";
            if (rowCurrency === currency || rowCurrency === "Unspecified") {
                total += numberOf(row[field]);
                continue;
            }
            excluded.set(rowCurrency, (excluded.get(rowCurrency) || 0) + numberOf(row[field]));
        }
        return total;
    }

    // The mean cost of turning over one room, across every configured room
    // category and occupancy. The cost facts carry a total departure count
    // only - no per-category, per-occupancy breakdown - so a single blended
    // rate is the most the data supports. The caller flags this.
    function averageCleaningCost(cleaningCategories, costPerMinute) {
        const rows = cleaningCategories || [];
        if (!rows.length) return null;
        const total = rows.reduce((running, row) =>
            running + numberOf(row.cleaningMinutes) * numberOf(costPerMinute)
                + numberOf(row.linenCost), 0);
        return total / rows.length;
    }

    function zeroTotals() {
        const totals = {};
        for (const {key} of GOP_LINES) totals[key] = 0;
        return totals;
    }

    // What the franchise percentage is charged on. Every revenue column in the
    // cost facts is net of VAT, so these are all net figures; the gross basis
    // grosses the chosen one up by the property's VAT rate rather than reaching
    // for gross payments, which are a different quantity entirely (they include
    // deposits, other services and anything settled in the period).
    const FRANCHISE_REVENUE_BASES = Object.freeze({
        roomInclProducts: Object.freeze({
            label: "Room revenue incl. products",
            of: (r) => r.roomRevenue
        }),
        roomExclProducts: Object.freeze({
            label: "Room revenue excl. products",
            of: (r) => r.roomRevenueExclProducts
        }),
        roomExclProductsPlusParking: Object.freeze({
            label: "Room revenue excl. products, plus parking",
            of: (r) => r.roomRevenueExclProducts + r.parkingRevenue
        }),
        totalRevenue: Object.freeze({
            label: "Total revenue",
            of: (r) => r.roomRevenue + r.parkingRevenue
        })
    });

    function franchiseBaseAmount(profile, revenue) {
        const base = FRANCHISE_REVENUE_BASES[profile.franchiseRevenueBase]
            || FRANCHISE_REVENUE_BASES.roomInclProducts;
        const net = base.of(revenue);
        return profile.franchiseBasis === "gross"
            ? net * (1 + numberOf(profile.franchiseVatPercent) / 100)
            : net;
    }

    // Unrounded totals for one set of rows. Separated from calculateGop so the
    // per-period chart and the scope-wide statement run the same arithmetic:
    // every threshold is still evaluated per stay date, and a stay date belongs
    // to exactly one period, so the periods partition the scope exactly.
    //
    // Their *rounded* figures can still differ from the statement's by a krona
    // or two, because each period is rounded to whole kronor on its own and the
    // statement rounds the scope total once. The statement is the authority;
    // the chart is a shape, and half a krona per bar is not visible on it.
    function accumulate(source, hotels, settingsIndex, flag, currencies) {
        const totals = zeroTotals();

        for (const hotel of hotels) {
            const settings = settingsIndex[hotel];
            const profile = (settings && settings.profile) || {};
            const currency = profile.currency || "SEK";
            currencies.add(currency);

            const roomRevenueRows = filterRows(source.roomRevenue, hotel);
            const paymentRows = filterRows(source.payments, hotel);
            const breakfastRows = filterRows(source.breakfast, hotel);
            const parkingRows = filterRows(source.parking, hotel);
            const movementRows = filterRows(source.arrivalsDepartures, hotel);

            const excluded = new Map();
            const roomRevenue = currencyTotal(
                roomRevenueRows, "roomRevenueInclProducts1Net", currency, excluded
            );
            // Needed on its own for the franchise basis "room revenue minus
            // products", which is the room line with breakfast and every other
            // sold product taken out of it.
            const roomRevenueExclProducts = currencyTotal(
                roomRevenueRows, "roomRevenueExclProducts1Net", currency, new Map()
            );
            const paymentsGross = currencyTotal(
                paymentRows, "totalPaymentAmountGrossValue", currency, excluded
            );
            for (const [otherCurrency, amount] of excluded) {
                flag(
                    `${hotel}: ${Format.formatSek(amount)} ${otherCurrency} was excluded - `
                    + `this property's Cost Input currency is ${currency}.`
                );
            }

            // Parking and breakfast facts carry no currency column, so they are
            // taken as the property's own currency.
            const parkingRevenue = sum(parkingRows, "totalParkingAmountNetValue");
            const breakfastRevenue = sum(breakfastRows, "breakfastNetCost");

            totals.roomRevenue += roomRevenue;
            totals.parkingRevenue += parkingRevenue;

            if (!settings) {
                flag(
                    `${hotel}: no Cost Input configuration was found, so its revenue is `
                    + "included but none of its costs are. Open Cost Input and save this "
                    + "property's settings."
                );
                continue;
            }

            // --- Breakfast: food per guest + staffing from the thresholds ----
            const guestsPerDay = dailyTotals(breakfastRows, "breakfastTotal");
            const breakfastTiers = settings.breakfastTiers || [];
            let breakfastGuests = 0;
            let breakfastStaffHours = 0;
            for (const guests of guestsPerDay.values()) {
                breakfastGuests += guests;
                if (guests <= 0) continue;
                const tier = matchTier(breakfastTiers, guests, "minGuests", "maxGuests");
                if (tier) breakfastStaffHours += numberOf(tier.staffHours);
            }
            if (profile.breakfastCalculationBasis === "products") {
                flag(
                    `${hotel}: breakfast is configured to calculate from sold products, but `
                    + "the cost facts expose only a breakfast count. The guest count was used "
                    + "for both the food cost and the staffing threshold."
                );
            }
            if (!breakfastTiers.length && breakfastGuests > 0) {
                flag(
                    `${hotel}: no breakfast staffing thresholds are configured, so breakfast `
                    + "cost covers food only."
                );
            }
            totals.breakfastCost +=
                breakfastGuests * numberOf(profile.breakfastFoodCostPerGuest)
                + breakfastStaffHours * numberOf(profile.breakfastStaffCostPerHour);

            // --- Distribution --------------------------------------------------
            totals.distributionCost += percentOf(
                roomRevenue, profile.distributionDefaultPercent
            );
            const hasDistributionTree = (settings.distributionOriginGroups || []).length
                || (settings.distributionGroups || []).length;
            if (hasDistributionTree) {
                flag(
                    `${hotel}: the fallback distribution % was applied to all room revenue. `
                    + "The per-origin, per-agency and per-rate percentages need a reservation "
                    + "level breakdown, which the cost fact tables do not carry - they hold one "
                    + "revenue total per stay date."
                );
            }

            // --- Franchise & card ----------------------------------------------
            // The franchise fee on its configured revenue base, the card cost
            // percentage on gross payments, and the three rent percentages on
            // their matching net revenue stream. Franchise is skipped entirely
            // when the property has it switched off, rather than costed at 0%,
            // so an unconfigured percentage cannot quietly become a real one.
            let franchiseCost = 0;
            if (isEnabled(profile.franchiseEnabled, false)) {
                franchiseCost = percentOf(
                    franchiseBaseAmount(profile, {
                        roomRevenue,
                        roomRevenueExclProducts,
                        parkingRevenue
                    }),
                    profile.franchisePercent
                );
                if (profile.franchiseBasis === "gross") {
                    flag(
                        `${hotel}: the franchise fee is charged on a gross basis, so its net `
                        + `revenue base was grossed up by ${numberOf(profile.franchiseVatPercent)}% `
                        + "VAT. Every revenue column in the cost facts is net of VAT."
                    );
                }
            }
            totals.franchiseCardCost +=
                franchiseCost
                + percentOf(roomRevenue, profile.roomRentPercent)
                + percentOf(breakfastRevenue, profile.breakfastRentPercent)
                + percentOf(parkingRevenue, profile.parkingRentPercent)
                + percentOf(paymentsGross, profile.cardCostPercent);
            if (numberOf(profile.breakfastRentPercent) > 0) {
                flag(
                    `${hotel}: breakfast rent % was applied to the source column `
                    + "breakfast_net_cost, the only breakfast money figure in the cost facts. "
                    + "Confirm it is breakfast revenue and not a cost before relying on this line."
                );
            }

            // --- Cleaning --------------------------------------------------------
            const departures = sum(movementRows, "totalDepartures");
            const perDeparture = averageCleaningCost(
                settings.cleaningCategories, profile.cleaningCostPerMinute
            );
            if (perDeparture === null) {
                if (departures > 0) {
                    flag(
                        `${hotel}: no cleaning rows are configured for its room categories, so `
                        + "cleaning cost is zero. Set cleaning minutes and linen cost in Cost Input."
                    );
                }
            }
            else {
                totals.cleaningCost += departures * perDeparture;
                flag(
                    "Cleaning cost is departures x the average of the configured category and "
                    + "occupancy rows. The cost facts carry a total departure count only, with "
                    + "no per-category or per-occupancy breakdown."
                );
            }

            // --- Arrivals ---------------------------------------------------------
            // A property that does not staff reception by arrival volume turns
            // this off. That is a decision, so it produces neither a cost nor a
            // warning - unlike an empty threshold list, which is an omission.
            if (!isEnabled(profile.arrivalCostEnabled)) continue;

            const arrivalsPerDay = dailyTotals(movementRows, "totalArrivals");
            const arrivalTiers = settings.arrivalTiers || [];
            let receptionHours = 0;
            let totalArrivals = 0;
            for (const arrivals of arrivalsPerDay.values()) {
                totalArrivals += arrivals;
                if (arrivals <= 0) continue;
                const tier = matchTier(arrivalTiers, arrivals, "minArrivals", "maxArrivals");
                if (tier) receptionHours += numberOf(tier.receptionHours);
            }
            if (!arrivalTiers.length && totalArrivals > 0) {
                flag(
                    `${hotel}: no reception staffing thresholds are configured, so arrival cost `
                    + "is zero. Switch arrival cost off in Cost Input if that is deliberate."
                );
            }
            totals.arrivalCost += receptionHours * numberOf(profile.receptionCostPerHour);
        }

        return totals;
    }

    // Rounds one set of unrounded totals into the statement rows. Each line is
    // rounded to whole kronor once and GOP is derived from the rounded lines,
    // so the statement always adds up on screen.
    function toAmounts(totals) {
        const amounts = {};
        for (const {key} of GOP_LINES) {
            if (key === "gop") continue;
            amounts[key] = Format.roundSek(totals[key]) || 0;
        }
        amounts.gop = REVENUE_KEYS.reduce((running, key) => running + amounts[key], 0)
            - COST_KEYS.reduce((running, key) => running + amounts[key], 0);
        return amounts;
    }

    // The same rows, split into the buckets the chart draws. Every dataset is
    // partitioned by the period its stay date falls in, so no row is counted
    // twice and none is dropped.
    function splitByPeriod(source, grain) {
        const buckets = new Map();
        for (const [dataset, rows] of Object.entries(source || {})) {
            for (const row of rows || []) {
                if (!row || !row.stayDate) continue;
                const key = periodKey(row.stayDate, grain);
                let bucket = buckets.get(key);
                if (!bucket) {
                    bucket = {};
                    buckets.set(key, bucket);
                }
                (bucket[dataset] || (bucket[dataset] = [])).push(row);
            }
        }
        return new Map([...buckets].sort(
            ([left], [right]) => left.localeCompare(right)
        ));
    }

    function calculateGop(
        data, { hotelName = "", settingsByHotel = {}, grain = "" } = {}
    ) {
        const source = data || {};
        const settingsIndex = settingsByHotel || {};
        const hotels = hotelsInScope(source, hotelName);
        const flags = [];
        const seenFlags = new Set();
        const currencies = new Set();

        function flag(message) {
            if (seenFlags.has(message)) return;
            seenFlags.add(message);
            flags.push(message);
        }

        const totals = accumulate(source, hotels, settingsIndex, flag, currencies);
        const amounts = toAmounts(totals);

        // Periods are only computed when a grain is asked for: the statement
        // itself needs one set of numbers, and bucketing every dataset is not
        // free on a year of daily rows.
        const periods = [];
        if (grain) {
            const ignore = () => {};
            for (const [key, bucket] of splitByPeriod(source, grain)) {
                const bucketTotals = accumulate(
                    bucket, hotelsInScope(bucket, hotelName), settingsIndex,
                    ignore, new Set()
                );
                const bucketAmounts = toAmounts(bucketTotals);
                periods.push({
                    periodKey: key,
                    amounts: bucketAmounts,
                    revenue: REVENUE_KEYS.reduce(
                        (running, name) => running + bucketAmounts[name], 0
                    ),
                    cost: COST_KEYS.reduce(
                        (running, name) => running + bucketAmounts[name], 0
                    ),
                    gop: bucketAmounts.gop
                });
            }
        }

        return {
            currency: currencies.size === 1 ? Array.from(currencies)[0] : "SEK",
            hotels,
            lines: GOP_LINES.map((line) => ({...line, amount: amounts[line.key]})),
            gop: amounts.gop,
            periods,
            flags
        };
    }

    const api = {
        DATASET_RULES,
        GOP_LINES,
        FRANCHISE_REVENUE_BASES,
        periodKey,
        aggregate,
        summarize,
        calculateGop,
        // Exported for the tests: the nearest-band fallback and the checkbox
        // coercion are both easy to get subtly wrong and neither is reachable
        // through calculateGop without building a whole fixture.
        matchTier,
        isEnabled
    };
    if (typeof module === "object" && module.exports) module.exports = api;
    root.CostData = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
