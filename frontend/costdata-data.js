(function initializeCostData(root) {
    "use strict";

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

    function periodKey(dateValue, grain) {
        if (grain === "year") return `${dateValue.slice(0, 4)}-01-01`;
        if (grain === "month") return `${dateValue.slice(0, 7)}-01`;
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

    const api = { DATASET_RULES, periodKey, aggregate, summarize };
    if (typeof module === "object" && module.exports) module.exports = api;
    root.CostData = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
