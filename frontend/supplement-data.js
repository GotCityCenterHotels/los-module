(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    else root.SupplementData = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";

    const DAY_MS = 86_400_000;
    const MAX_RANGE_DAYS = 31;
    const HOTELS = Object.freeze([
        { code: "hotel-a", name: "Hotel A" },
        { code: "hotel-b", name: "Hotel B" },
        { code: "hotel-vasa", name: "Hotel Vasa" }
    ]);
    const CATEGORIES = Object.freeze([
        { code: "standard", shortName: "STD", name: "Standard", inventory: 42 },
        { code: "superior", shortName: "SUP", name: "Superior", inventory: 28 },
        { code: "deluxe", shortName: "DLX", name: "Deluxe", inventory: 16 },
        { code: "suite", shortName: "STE", name: "Suite", inventory: 8 }
    ]);
    const METRICS = Object.freeze(["occ", "adr", "revpar"]);

    function parseDateKey(value) {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value || ""))) return null;
        const date = new Date(`${value}T00:00:00Z`);
        return Number.isNaN(date.getTime()) || formatDateKey(date) !== value ? null : date;
    }

    function formatDateKey(date) {
        return date.toISOString().slice(0, 10);
    }

    function addDays(date, amount) {
        return new Date(date.getTime() + amount * DAY_MS);
    }

    function getComparisonDate(date, basis) {
        if (basis === "sameWeekday") return addDays(date, -364);
        const targetYear = date.getUTCFullYear() - 1;
        const month = date.getUTCMonth();
        const lastDayOfMonth = new Date(Date.UTC(targetYear, month + 1, 0)).getUTCDate();
        return new Date(Date.UTC(targetYear, month, Math.min(date.getUTCDate(), lastDayOfMonth)));
    }

    function validateDateRange(startDate, endDate) {
        const start = parseDateKey(startDate);
        const end = parseDateKey(endDate);
        if (!start || !end) return { valid: false, error: "Enter a valid start and end date.", dayCount: 0 };
        const dayCount = Math.round((end - start) / DAY_MS) + 1;
        if (dayCount < 1) return { valid: false, error: "Start date cannot be after end date.", dayCount };
        if (dayCount > MAX_RANGE_DAYS) {
            return { valid: false, error: `Preview ranges are limited to ${MAX_RANGE_DAYS} days.`, dayCount };
        }
        return { valid: true, error: null, dayCount };
    }

    function dateRange(startDate, endDate) {
        const validation = validateDateRange(startDate, endDate);
        if (!validation.valid) return [];
        const start = parseDateKey(startDate);
        return Array.from({ length: validation.dayCount }, (_, index) => addDays(start, index));
    }

    function hashText(value) {
        let hash = 2166136261;
        for (const character of String(value)) {
            hash ^= character.charCodeAt(0);
            hash = Math.imul(hash, 16777619);
        }
        return hash >>> 0;
    }

    function unitValue(seed) {
        return (hashText(seed) % 10_000) / 10_000;
    }

    function calculateMetrics(facts) {
        const inventory = Number(facts?.inventory) || 0;
        const roomsSold = Number(facts?.roomsSold) || 0;
        const revenue = Number(facts?.revenue) || 0;
        return {
            occ: inventory > 0 ? roomsSold / inventory * 100 : null,
            adr: roomsSold > 0 ? revenue / roomsSold : null,
            revpar: inventory > 0 ? revenue / inventory : null
        };
    }

    function createFacts(hotel, category, dateKey, mode, basis) {
        const hotelIndex = HOTELS.findIndex(({ code }) => code === hotel.code);
        const categoryIndex = CATEGORIES.findIndex(({ code }) => code === category.code);
        const comparisonKey = mode === "today"
            ? dateKey
            : formatDateKey(getComparisonDate(parseDateKey(dateKey), basis));
        const demand = unitValue(`${hotel.code}|${category.code}|${comparisonKey}|${mode}|demand`);
        const rateNoise = unitValue(`${hotel.code}|${category.code}|${comparisonKey}|${mode}|rate`);
        const inventory = category.inventory + hotelIndex * 3;
        const modeAdjustment = mode === "spit" ? -0.045 : mode === "ly" ? -0.025 : 0;
        const occupancy = Math.max(0.24, Math.min(0.97, 0.48 + demand * 0.44 + modeAdjustment));
        const roomsSold = Math.min(inventory, Math.round(inventory * occupancy));
        const baseRate = 920 + hotelIndex * 105 + categoryIndex * 310;
        const adr = baseRate * (0.9 + rateNoise * 0.24) * (mode === "today" ? 1.04 : 1);
        return {
            inventory,
            roomsSold,
            revenue: Math.round(roomsSold * adr),
            metrics: null
        };
    }

    function withMetrics(facts) {
        return { ...facts, metrics: calculateMetrics(facts) };
    }

    function generateDataset(options) {
        const startDate = options?.startDate;
        const endDate = options?.endDate;
        const basis = options?.lyComparisonType === "sameWeekday" ? "sameWeekday" : "sameDate";
        const validation = validateDateRange(startDate, endDate);
        if (!validation.valid) return { validation, dates: [], records: [] };
        const todayKey = options?.today && parseDateKey(options.today) ? options.today : formatDateKey(new Date());
        const dates = dateRange(startDate, endDate).map((date) => ({
            date: formatDateKey(date),
            lyDate: formatDateKey(getComparisonDate(date, basis)),
            isPast: formatDateKey(date) < todayKey,
            isWeekend: [0, 6].includes(date.getUTCDay())
        }));
        const records = [];
        for (const hotel of HOTELS) {
            for (const category of CATEGORIES) {
                records.push({
                    hotel,
                    category,
                    cells: dates.map(({ date }) => ({
                        today: withMetrics(createFacts(hotel, category, date, "today", basis)),
                        ly: withMetrics(createFacts(hotel, category, date, "ly", basis)),
                        spit: withMetrics(createFacts(hotel, category, date, "spit", basis))
                    }))
                });
            }
        }
        return { validation, dates, records };
    }

    function sumFacts(items) {
        return withMetrics(items.reduce((total, item) => ({
            inventory: total.inventory + item.inventory,
            roomsSold: total.roomsSold + item.roomsSold,
            revenue: total.revenue + item.revenue
        }), { inventory: 0, roomsSold: 0, revenue: 0 }));
    }

    function aggregateRecords(records, label, rowType, code, isTotal) {
        const cellCount = records[0]?.cells.length || 0;
        return {
            rowType,
            code,
            label,
            isTotal: Boolean(isTotal),
            cells: Array.from({ length: cellCount }, (_, index) => ({
                today: sumFacts(records.map((record) => record.cells[index].today)),
                ly: sumFacts(records.map((record) => record.cells[index].ly)),
                spit: sumFacts(records.map((record) => record.cells[index].spit))
            }))
        };
    }

    function buildRows(dataset, options) {
        const mode = options?.mode === "comparison" ? "comparison" : "single";
        if (!dataset?.validation?.valid) return [];
        if (mode === "comparison") {
            const enabled = new Set(options?.enabledHotels || HOTELS.map(({ code }) => code));
            const hotelRows = HOTELS.filter(({ code }) => enabled.has(code)).map((hotel) => {
                const records = dataset.records.filter((record) => record.hotel.code === hotel.code);
                return aggregateRecords(records, hotel.name, "hotel", hotel.code, false);
            });
            const totalRecords = dataset.records.filter((record) => enabled.has(record.hotel.code));
            if (totalRecords.length) hotelRows.push(aggregateRecords(totalRecords, "Selected hotels", "hotel", "total", true));
            return hotelRows;
        }
        const hotelCode = options?.hotelCode || HOTELS[0].code;
        const enabled = new Set(options?.enabledCategories || CATEGORIES.map(({ code }) => code));
        const source = dataset.records.filter((record) => record.hotel.code === hotelCode);
        const rows = source.filter((record) => enabled.has(record.category.code)).map((record) => ({
            rowType: "category",
            code: record.category.code,
            label: record.category.name,
            shortLabel: record.category.shortName,
            isTotal: false,
            cells: record.cells
        }));
        const totalRecords = source.filter((record) => enabled.has(record.category.code));
        if (totalRecords.length) rows.push(aggregateRecords(totalRecords, "Selected categories", "category", "total", true));
        return rows;
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
            output[mode] = sumFacts(row.cells.map((cell) => cell[mode])).metrics;
        }
        return output;
    }

    function getDetailData(options) {
        const hotel = HOTELS.find(({ code }) => code === options.hotelCode) || HOTELS[0];
        const category = CATEGORIES.find(({ code }) => code === options.categoryCode) || null;
        const seed = `${hotel.code}|${category?.code || "all"}|${options.date}|${options.metric}`;
        const names = ["Direct", "Flexible", "Advance purchase", "Package"];
        const weights = names.map((name) => 0.3 + unitValue(`${seed}|${name}`));
        const weightTotal = weights.reduce((sum, value) => sum + value, 0);
        const totalRooms = category?.inventory || CATEGORIES.reduce((sum, item) => sum + item.inventory, 0);
        const breakdown = names.map((name, index) => {
            const share = weights[index] / weightTotal;
            return {
                name,
                rooms: Math.max(1, Math.round(totalRooms * share * 0.78)),
                share: share * 100,
                averagePrice: Math.round(850 + index * 175 + unitValue(`${seed}|price|${index}`) * 280)
            };
        });
        const curve = Array.from({ length: 13 }, (_, index) => {
            const daysBeforeStay = 180 - index * 15;
            const progress = index / 12;
            const capacity = totalRooms;
            const today = Math.round(capacity * Math.min(0.96, 0.08 + progress * (0.62 + unitValue(`${seed}|curve`) * 0.22)));
            const comparison = Math.round(capacity * Math.min(0.94, 0.1 + progress * (0.57 + unitValue(`${seed}|comparison`) * 0.2)));
            return { daysBeforeStay, today, comparison };
        });
        return { hotel, category, breakdown, curve };
    }

    return {
        MAX_RANGE_DAYS,
        HOTELS,
        CATEGORIES,
        METRICS,
        parseDateKey,
        formatDateKey,
        getComparisonDate,
        validateDateRange,
        calculateMetrics,
        generateDataset,
        buildRows,
        calculateDifference,
        formatMetric,
        formatDifference,
        computeRowAverages,
        getDetailData
    };
}));
