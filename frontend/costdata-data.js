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
        Object.freeze({key: "rentCost", label: "Rent cost", type: "cost"}),
        Object.freeze({key: "cleaningCost", label: "Cleaning cost", type: "cost"}),
        Object.freeze({key: "arrivalCost", label: "Arrival cost", type: "cost"}),
        Object.freeze({key: "gop", label: "Gross Operating Profit (GOP)", type: "result"})
    ]);

    const REVENUE_KEYS = GOP_LINES.filter(({type}) => type === "revenue").map(({key}) => key);
    const COST_KEYS = GOP_LINES.filter(({type}) => type === "cost").map(({key}) => key);
    // Everything the operator can switch on and off. GOP is the result of the
    // ones that are on, so it is never one of them.
    const TOGGLEABLE_KEYS = Object.freeze([...REVENUE_KEYS, ...COST_KEYS]);

    // Which lines take part in this reading of the statement. Anything absent
    // from the caller's list is left out of the rows, out of GOP, and out of
    // the chart's bars - an omitted list means all of them, so the default is
    // always the whole statement.
    function activeLineSet(activeLines) {
        if (!activeLines) return new Set(TOGGLEABLE_KEYS);
        const wanted = new Set(activeLines);
        return new Set(TOGGLEABLE_KEYS.filter((key) => wanted.has(key)));
    }

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

    const UNSPECIFIED = "Unspecified";

    // localeCompare rebuilds its collator from the arguments on every call, and
    // a comparator runs tens of thousands of times on a full year. No locale
    // argument, so the ordering matches the localeCompare() calls it replaces.
    const collator = new Intl.Collator();

    function childMap(parent, key) {
        let child = parent.get(key);
        if (child === undefined) {
            child = new Map();
            parent.set(key, child);
        }
        return child;
    }

    function aggregate(dataset, rows, { grain = "day", hotelName = "" } = {}) {
        const rule = DATASET_RULES[dataset];
        if (!rule) throw new Error(`Unknown cost dataset: ${dataset}`);

        // One Map level per grouping field, keyed on the strings the rows
        // already carry, rather than one composite key string built and hashed
        // end to end for every row. Same reasoning - and the same measured
        // difference - as the LOS fact grouping in los-data.js.
        const index = new Map();
        const grouped = [];
        // A dataset repeats each stay date across hotels and dimensions, and at
        // a week grain resolving one costs a Date round trip.
        const periodKeys = new Map();

        for (const row of filterRows(rows, hotelName)) {
            let stayDate = periodKeys.get(row.stayDate);
            if (stayDate === undefined) {
                stayDate = periodKey(row.stayDate, grain);
                periodKeys.set(row.stayDate, stayDate);
            }

            // The grouping path: period, hotel, then whatever this dataset is
            // dimensioned by. Every element but the last addresses a nested
            // Map; the last addresses the group itself.
            const path = [stayDate, row.hotelName || UNSPECIFIED];
            for (const dimension of rule.dimensions) {
                path.push(row[dimension] || UNSPECIFIED);
            }
            let bucket = index;
            for (let depth = 0; depth < path.length - 1; depth += 1) {
                bucket = childMap(bucket, path[depth]);
            }
            const leaf = path[path.length - 1];
            let result = bucket.get(leaf);

            if (result === undefined) {
                result = {
                    stayDate,
                    hotelName: row.hotelName || UNSPECIFIED,
                    lastUpdatedAt: row.lastUpdatedAt || null
                };
                for (const dimension of rule.dimensions) {
                    result[dimension] = row[dimension] || UNSPECIFIED;
                }
                for (const value of rule.values) result[value] = 0;
                bucket.set(leaf, result);
                grouped.push(result);
            }

            for (const value of rule.values) result[value] += Number(row[value]) || 0;
            if (row.lastUpdatedAt && (!result.lastUpdatedAt || row.lastUpdatedAt > result.lastUpdatedAt)) {
                result.lastUpdatedAt = row.lastUpdatedAt;
            }
        }

        // Stay date first, then hotel, then the dimensions that actually
        // distinguish two rows sharing both. The tiebreak used to serialise
        // whole row objects and compare the JSON text, which ordered ties by
        // lastUpdatedAt - an accident of key order, not a decision - and cost
        // two full stringifies per comparison.
        return grouped.sort((left, right) => {
            if (left.stayDate !== right.stayDate) {
                return left.stayDate < right.stayDate ? -1 : 1;
            }
            const byHotel = collator.compare(left.hotelName, right.hotelName);
            if (byHotel !== 0) return byHotel;
            for (const dimension of rule.dimensions) {
                const byDimension = collator.compare(
                    String(left[dimension]), String(right[dimension])
                );
                if (byDimension !== 0) return byDimension;
            }
            return 0;
        });
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

    // The same rows under the same currency rule as currencyTotal, bucketed by
    // stay date. The two have to agree exactly: this is what a day's own
    // distribution percentage is charged on, and their sum is the room revenue
    // line the statement shows.
    function currencyDailyTotals(rows, field, currency) {
        const totals = new Map();
        for (const row of rows) {
            const rowCurrency = row.amountCurrency || "Unspecified";
            if (rowCurrency !== currency && rowCurrency !== "Unspecified") continue;
            totals.set(
                row.stayDate,
                (totals.get(row.stayDate) || 0) + numberOf(row[field])
            );
        }
        return totals;
    }

    // What turning over one room costs, for one configured (category, occupancy)
    // row.
    //
    // The effective figures are the ones inheritance has already resolved: a row
    // that takes its bed setup or its minutes from the category's lowest
    // occupancy has no figures of its own, and reading its raw fields would cost
    // it at zero. Older payloads carry only the raw fields, so those still stand
    // in.
    function rowCleaningCost(row, costPerMinute) {
        return numberOf(row.effectiveCleaningMinutes ?? row.cleaningMinutes)
            * numberOf(costPerMinute)
            + numberOf(row.effectiveLinenCost ?? row.linenCost);
    }

    // The mean cost of turning over one room, across every configured row,
    // weighting each equally. Used only where the departure mix is unavailable -
    // it treats a rarely sold suite as heavily as the double room the property
    // mostly sells, which is exactly what the mix exists to correct.
    function averageCleaningCost(cleaningCategories, costPerMinute) {
        const rows = cleaningCategories || [];
        if (!rows.length) return null;
        const total = rows.reduce(
            (running, row) => running + rowCleaningCost(row, costPerMinute), 0
        );
        return total / rows.length;
    }

    function cleaningKey(categoryName, occupancy) {
        return `${String(categoryName ?? "").trim().toLowerCase()}|${Number(occupancy)}`;
    }

    function cleaningCostIndex(cleaningCategories, costPerMinute) {
        const index = new Map();
        for (const row of cleaningCategories || []) {
            index.set(
                cleaningKey(row.categoryName, row.occupancy),
                rowCleaningCost(row, costPerMinute)
            );
        }
        return index;
    }

    // The guest counts configured for each category, ascending.
    function occupancyIndex(cleaningCategories) {
        const index = new Map();
        for (const row of cleaningCategories || []) {
            const category = String(row.categoryName ?? "").trim().toLowerCase();
            if (!index.has(category)) index.set(category, []);
            index.get(category).push(Number(row.occupancy));
        }
        for (const counts of index.values()) counts.sort((left, right) => left - right);
        return index;
    }

    // A guest count with no row of its own takes the nearest one below it, and
    // the lowest when it clears none. Same nearest-band rule as matchTier, for
    // the same reason: a property that configured 1 to 3 guests and then houses a
    // fourth has not said a fourth guest costs nothing to clean up after.
    function nearestOccupancy(occupancies, categoryName, occupancy) {
        const configured = occupancies.get(
            String(categoryName ?? "").trim().toLowerCase()
        );
        if (!configured || !configured.length) return null;
        let below = null;
        for (const value of configured) {
            if (value <= occupancy && (below === null || value > below)) below = value;
        }
        return below === null ? configured[0] : below;
    }

    // The cost of turning over one room, weighted by how the period's departures
    // actually split across room category and guest count.
    //
    // This is the whole point of the departure mix: the property's own mix is
    // what decides whether a period leans on its cheapest category or its most
    // expensive one, and no amount of configuration could express that from the
    // hotel-per-day totals the page used to have.
    //
    // Returns null when there is no mix for these rows, so the caller can fall
    // back to the flat average and say why.
    function mixedCleaningCost(mixRows, cleaningCategories, costPerMinute) {
        const rows = mixRows || [];
        if (!rows.length) return null;
        const costs = cleaningCostIndex(cleaningCategories, costPerMinute);
        if (!costs.size) return null;
        const occupancies = occupancyIndex(cleaningCategories);
        const blended = averageCleaningCost(cleaningCategories, costPerMinute) || 0;

        let departures = 0;
        let cost = 0;
        const unconfigured = new Set();
        for (const row of rows) {
            const count = numberOf(row.departures);
            if (count <= 0) continue;
            departures += count;

            const exact = costs.get(cleaningKey(row.categoryName, row.occupancy));
            if (exact !== undefined) {
                cost += count * exact;
                continue;
            }
            const nearest = nearestOccupancy(
                occupancies, row.categoryName, numberOf(row.occupancy)
            );
            const nearestCost = nearest === null
                ? undefined
                : costs.get(cleaningKey(row.categoryName, nearest));
            if (nearestCost !== undefined) {
                cost += count * nearestCost;
                continue;
            }
            // The room category itself has no cleaning rows at all. Costing
            // these departures at zero would understate the period silently, so
            // they take the property's average and the caller names the category.
            unconfigured.add(String(row.categoryName ?? "").trim() || "(unnamed)");
            cost += count * blended;
        }
        if (departures <= 0) return null;
        return {
            costPerDeparture: cost / departures,
            unconfigured: Array.from(unconfigured).sort()
        };
    }

    // The distribution percentage for one hotel-day, blending the share of
    // revenue the rulebook matched with the property's fallback for the rest.
    //
    // The matching itself happens in SQL, where the rulebook and the mix live
    // together; the fallback stays here so it is defined in exactly one place.
    function effectiveDistributionPercent(rate, fallbackPercent) {
        const fallback = numberOf(fallbackPercent);
        if (!rate) return fallback;
        const mixRevenue = numberOf(rate.mixRevenue);
        if (mixRevenue === 0) return fallback;
        // A correction period can carry negative revenue, which would otherwise
        // produce a share outside 0-1 and a percentage outside both inputs.
        const matchedShare = Math.min(
            1, Math.max(0, numberOf(rate.matchedRevenue) / mixRevenue)
        );
        const matched = rate.matchedPercent === null || rate.matchedPercent === undefined
            ? fallback
            : numberOf(rate.matchedPercent);
        return matchedShare * matched + (1 - matchedShare) * fallback;
    }

    // -----------------------------------------------------------------------
    // Comparing with last year
    //
    // Last year's rows, restamped with the current-year dates they compare
    // against, so both years bucket into the same periods by construction and can
    // be matched on the period key.
    //
    // The alternative - bucketing each year separately and pairing bar 1 with bar
    // 1 - slips the moment one year has a period the other does not, and then
    // every bar after it is compared against the wrong month with nothing on
    // screen to show it.
    //
    // Restamping is safe because every figure on the statement is derived per
    // stay date and the shift is one-to-one on dates: the staffing thresholds
    // still band each day on its own, and the two mixes still meet the revenue
    // and departures they belong to, because they are shifted with them.
    //
    // hotelsInScope narrows it to the properties this year has. A hotel that
    // closed would otherwise put revenue in the last-year bar with nothing beside
    // it to compare against, and read as a collapse.
    function alignToComparison(source, basis, hotelsInScope) {
        const scope = hotelsInScope instanceof Set
            ? hotelsInScope
            : new Set(hotelsInScope || []);
        const aligned = {};
        for (const [dataset, rows] of Object.entries(source || {})) {
            aligned[dataset] = (rows || [])
                .filter((row) => row && row.stayDate
                    && (!row.hotelName || scope.has(row.hotelName)))
                .map((row) => ({
                    ...row, stayDate: Format.thisYearDate(row.stayDate, basis)
                }));
        }
        return aligned;
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
                    + "for both the food cost and the staffing threshold.",
                    "breakfastCost"
                );
            }
            if (!breakfastTiers.length && breakfastGuests > 0) {
                flag(
                    `${hotel}: no breakfast staffing thresholds are configured, so breakfast `
                    + "cost covers food only.",
                    "breakfastCost"
                );
            }
            totals.breakfastCost +=
                breakfastGuests * numberOf(profile.breakfastFoodCostPerGuest)
                + breakfastStaffHours * numberOf(profile.breakfastStaffCostPerHour);

            // --- Distribution --------------------------------------------------
            // Charged per stay date, at the percentage that day's own mix of
            // origins, travel agencies and rates works out to. The mix is matched
            // against the rulebook in SQL and arrives as one row per hotel per
            // day; what is not matched by any origin group is charged the
            // property's fallback, which is why the two figures travel together.
            const hasDistributionTree = (settings.distributionOriginGroups || []).length
                || (settings.distributionGroups || []).length;
            const distributionRates = filterRows(source.distributionRates, hotel);
            if (distributionRates.length) {
                const rateByDay = new Map(
                    distributionRates.map((row) => [row.stayDate, row])
                );
                const revenueByDay = currencyDailyTotals(
                    roomRevenueRows, "roomRevenueInclProducts1Net", currency
                );
                let unmixedRevenue = 0;
                for (const [day, amount] of revenueByDay) {
                    const rate = rateByDay.get(day);
                    if (!rate) unmixedRevenue += amount;
                    totals.distributionCost += percentOf(
                        amount,
                        effectiveDistributionPercent(
                            rate, profile.distributionDefaultPercent
                        )
                    );
                }
                const mixRevenue = distributionRates.reduce(
                    (running, row) => running + numberOf(row.mixRevenue), 0
                );
                const matchedRevenue = distributionRates.reduce(
                    (running, row) => running + numberOf(row.matchedRevenue), 0
                );
                // Both of these are actionable, which is the difference between
                // them and the flag they replaced: one says to add an origin to a
                // group, the other says a stretch of the period predates the mix.
                if (hasDistributionTree && mixRevenue > 0
                    && matchedRevenue < mixRevenue * 0.999) {
                    flag(
                        `${hotel}: ${Format.formatSek(mixRevenue - matchedRevenue)} of room `
                        + "revenue came from an origin no group covers, so it was charged the "
                        + "fallback distribution %. Add that origin to a group in Cost Input.",
                        "distributionCost"
                    );
                }
                if (unmixedRevenue > 0) {
                    flag(
                        `${hotel}: ${Format.formatSek(unmixedRevenue)} of room revenue falls on `
                        + "days with no imported reservation mix, so it was charged the fallback "
                        + "distribution %.",
                        "distributionCost"
                    );
                }
            }
            else {
                totals.distributionCost += percentOf(
                    roomRevenue, profile.distributionDefaultPercent
                );
                if (hasDistributionTree) {
                    flag(
                        `${hotel}: only the fallback distribution % was applied. The `
                        + "reservation mix your per-origin, per-agency and per-rate percentages "
                        + "are matched against has not been imported for this period.",
                        "distributionCost"
                    );
                }
            }

            // --- Franchise & card ----------------------------------------------
            // The franchise fee on its configured revenue base and the card cost
            // percentage on gross payments. Franchise is skipped entirely when
            // the property has it switched off, rather than costed at 0%, so an
            // unconfigured percentage cannot quietly become a real one.
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
                        + "VAT. Every revenue column in the cost facts is net of VAT.",
                        "franchiseCardCost"
                    );
                }
            }
            totals.franchiseCardCost +=
                franchiseCost
                + percentOf(paymentsGross, profile.cardCostPercent);

            // --- Rent ------------------------------------------------------------
            // The three rent percentages, each on its matching net revenue
            // stream. Rent is its own line rather than part of franchise & card:
            // it is a different agreement, it is usually the larger of the two,
            // and folding it in left the statement with no rent on it at all.
            totals.rentCost +=
                percentOf(roomRevenue, profile.roomRentPercent)
                + percentOf(breakfastRevenue, profile.breakfastRentPercent)
                + percentOf(parkingRevenue, profile.parkingRentPercent);
            if (numberOf(profile.breakfastRentPercent) > 0) {
                flag(
                    `${hotel}: breakfast rent % was applied to the breakfast net figure. `
                    + "Check that this figure is breakfast revenue, not a cost, before "
                    + "relying on this line.",
                    "rentCost"
                );
            }

            // --- Cleaning --------------------------------------------------------
            // Departures come from the movement facts, which are authoritative;
            // the departure mix says how they split across room category and
            // guest count, which is the pair the rulebook is configured per. The
            // count being charged for therefore never depends on the mix - only
            // the rate does.
            const departures = sum(movementRows, "totalDepartures");
            const perDeparture = averageCleaningCost(
                settings.cleaningCategories, profile.cleaningCostPerMinute
            );
            const mixed = mixedCleaningCost(
                filterRows(source.cleaningDepartures, hotel),
                settings.cleaningCategories,
                profile.cleaningCostPerMinute
            );
            if (perDeparture === null) {
                if (departures > 0) {
                    flag(
                        `${hotel}: no cleaning rows are configured for its room categories, so `
                        + "cleaning cost is zero. Set cleaning minutes and linen cost in Cost Input.",
                        "cleaningCost"
                    );
                }
            }
            else if (mixed === null) {
                totals.cleaningCost += departures * perDeparture;
                flag(
                    `${hotel}: cleaning cost is the flat average of its configured category and `
                    + "occupancy rows, multiplied by departures. The departure mix that would "
                    + "weight it by the rooms actually vacated has not been imported for this "
                    + "period.",
                    "cleaningCost"
                );
            }
            else {
                totals.cleaningCost += departures * mixed.costPerDeparture;
                if (mixed.unconfigured.length) {
                    flag(
                        `${hotel}: departures from ${mixed.unconfigured.join(", ")} were costed at `
                        + "this property's average, because that room category has no cleaning "
                        + "rows. Set its beds and minutes in Cost Input.",
                        "cleaningCost"
                    );
                }
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
                    + "is zero. Switch arrival cost off in Cost Input if that is deliberate.",
                    "arrivalCost"
                );
            }
            totals.arrivalCost += receptionHours * numberOf(profile.receptionCostPerHour);
        }

        return totals;
    }

    // Rounds one set of unrounded totals into the statement rows. Each line is
    // rounded to whole kronor once and GOP is derived from the rounded lines,
    // so the statement always adds up on screen. Every line is still computed
    // when some are switched off - only GOP is narrowed to the active ones, so
    // switching a line back on cannot change the figures on the others.
    function toAmounts(totals, active) {
        const amounts = {};
        for (const {key} of GOP_LINES) {
            if (key === "gop") continue;
            amounts[key] = Format.roundSek(totals[key]) || 0;
        }
        amounts.gop = sumActive(amounts, REVENUE_KEYS, active)
            - sumActive(amounts, COST_KEYS, active);
        return amounts;
    }

    function sumActive(amounts, keys, active) {
        return keys.reduce(
            (running, key) => (active.has(key) ? running + amounts[key] : running), 0
        );
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
        data, { hotelName = "", settingsByHotel = {}, grain = "", activeLines = null } = {}
    ) {
        const source = data || {};
        const settingsIndex = settingsByHotel || {};
        const hotels = hotelsInScope(source, hotelName);
        const active = activeLineSet(activeLines);
        const raised = [];
        const seenFlags = new Set();
        const currencies = new Set();

        // A flag belongs to the line it explains. A line that is switched off is
        // not on the statement, so a warning about how it was derived is noise
        // rather than something to act on; a flag with no line (a currency
        // exclusion, an unconfigured property) is about the scope and always
        // stands.
        function flag(message, lineKey) {
            if (seenFlags.has(message)) return;
            seenFlags.add(message);
            raised.push({message, lineKey});
        }

        const totals = accumulate(source, hotels, settingsIndex, flag, currencies);
        const amounts = toAmounts(totals, active);

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
                const bucketAmounts = toAmounts(bucketTotals, active);
                periods.push({
                    periodKey: key,
                    amounts: bucketAmounts,
                    revenue: sumActive(bucketAmounts, REVENUE_KEYS, active),
                    cost: sumActive(bucketAmounts, COST_KEYS, active),
                    gop: bucketAmounts.gop
                });
            }
        }

        return {
            currency: currencies.size === 1 ? Array.from(currencies)[0] : "SEK",
            hotels,
            lines: GOP_LINES
                .filter((line) => line.key === "gop" || active.has(line.key))
                .map((line) => ({...line, amount: amounts[line.key]})),
            gop: amounts.gop,
            periods,
            flags: raised
                .filter(({lineKey}) => !lineKey || active.has(lineKey))
                .map(({message}) => message)
        };
    }

    const api = {
        DATASET_RULES,
        GOP_LINES,
        TOGGLEABLE_KEYS,
        FRANCHISE_REVENUE_BASES,
        periodKey,
        aggregate,
        summarize,
        calculateGop,
        alignToComparison,
        // Exported for the tests: the nearest-band fallback and the checkbox
        // coercion are both easy to get subtly wrong and neither is reachable
        // through calculateGop without building a whole fixture.
        matchTier,
        isEnabled,
        // The two halves of the reservation-mix arithmetic. Both blend a matched
        // figure with a fallback, which is the part that is easy to get wrong in
        // a way no total would reveal.
        mixedCleaningCost,
        effectiveDistributionPercent
    };
    if (typeof module === "object" && module.exports) module.exports = api;
    root.CostData = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
