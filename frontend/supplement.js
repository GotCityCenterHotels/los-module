(function () {
    "use strict";

    const Data = window.SupplementData;
    const API_BASE_URL = "/api";
    const DATE_WINDOW_SIZE = 31;
    const metricLabels = { occ: "OCC", adr: "ADR", revpar: "RevPAR" };
    const elements = {
        hotel: document.getElementById("supplementHotel"),
        singleHotelControl: document.getElementById("singleHotelControl"),
        categoryControl: document.getElementById("categoryControl"),
        hotelVisibilityControl: document.getElementById("hotelVisibilityControl"),
        categoryOptions: document.getElementById("categoryOptions"),
        categorySelectionSummary: document.getElementById("categorySelectionSummary"),
        hotelVisibilityOptions: document.getElementById("hotelVisibilityOptions"),
        hotelSelectionSummary: document.getElementById("hotelSelectionSummary"),
        startDate: document.getElementById("supplementStartDate"),
        endDate: document.getElementById("supplementEndDate"),
        lyBasis: document.getElementById("supplementLyBasis"),
        inventoryBasis: document.getElementById("supplementInventoryBasis"),
        diffMode: document.getElementById("supplementDiffMode"),
        highlights: document.getElementById("metricHighlightOptions"),
        pastLyDiff: document.getElementById("pastLyDiff"),
        futureSpitDiff: document.getElementById("futureSpitDiff"),
        futureLyDiff: document.getElementById("futureLyDiff"),
        validation: document.getElementById("supplementValidation"),
        tableMount: document.getElementById("supplementTableMount"),
        gridAnnouncement: document.getElementById("gridAnnouncement"),
        rangeSummary: document.getElementById("rangeSummary"),
        freshness: document.getElementById("supplementFreshness"),
        dateWindowNav: document.getElementById("dateWindowNav"),
        dateWindowLabel: document.getElementById("dateWindowLabel"),
        previousDateWindow: document.getElementById("previousDateWindow"),
        nextDateWindow: document.getElementById("nextDateWindow"),
        dialog: document.getElementById("supplementDetailDialog"),
        detailTitle: document.getElementById("detailTitle"),
        detailContext: document.getElementById("detailContext"),
        detailError: document.getElementById("detailError"),
        detailRooms: document.getElementById("detailRooms"),
        detailAdr: document.getElementById("detailAdr"),
        detailInventory: document.getElementById("detailInventory"),
        detailInventoryLabel: document.getElementById("detailInventoryLabel"),
        detailOccupancy: document.getElementById("detailOccupancy"),
        detailBreakdown: document.getElementById("detailBreakdown"),
        pickupCurve: document.getElementById("pickupCurve"),
        pickupCoverage: document.getElementById("pickupCoverage"),
        pickupWindowValue: document.getElementById("pickupWindowValue"),
        pickupWindowSlider: document.getElementById("pickupWindowSlider"),
        pickupWindowUp: document.getElementById("pickupWindowUp"),
        pickupWindowDown: document.getElementById("pickupWindowDown"),
        pickupWindowAll: document.getElementById("pickupWindowAll"),
        pickupWindowHint: document.getElementById("pickupWindowHint"),
        pickupCurrentLabel: document.getElementById("pickupCurrentLabel"),
        pickupComparisonLabel: document.getElementById("pickupComparisonLabel"),
        closeDialog: document.getElementById("closeDetailDialog"),
        dialogFootnote: document.getElementById("dialogFootnote")
    };

    const stockholmDateParts = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Europe/Stockholm",
        year: "numeric",
        month: "2-digit",
        day: "2-digit"
    }).formatToParts(new Date());
    const datePart = (type) => stockholmDateParts.find((part) => part.type === type)?.value;
    const todayKey = `${datePart("year")}-${datePart("month")}-${datePart("day")}`;
    const todayUtc = Data.parseDateKey(todayKey);
    const state = {
        mode: "single",
        hotels: [],
        categoriesByHotel: {},
        hotelCode: "",
        enabledCategories: new Set(),
        enabledHotels: new Set(),
        highlightedMetrics: new Set(["occ"]),
        startDate: Data.formatDateKey(new Date(todayUtc.getTime() - 3 * 86_400_000)),
        endDate: Data.formatDateKey(new Date(todayUtc.getTime() + 3 * 86_400_000)),
        lyComparisonType: "sameDate",
        inventoryBasis: "sellable",
        // Pickup lookback in days before the stay date; null means the complete
        // history back to the first booking.
        pickupWindowDays: 30,
        pickupWindowAll: false,
        pickupRequest: null,
        differenceMode: "percent",
        pastLyDiff: true,
        futureSpitDiff: false,
        futureLyDiff: false,
        requestId: 0,
        payload: null,
        windowStart: 0
    };

    const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };

    // One pass instead of five, and this runs on every cell's accessible name.
    function escapeHtml(value) {
        return String(value ?? "").replace(/[&<>"']/g, (character) => HTML_ESCAPES[character]);
    }

    const dateLabels = new Map();

    // toLocaleDateString parses options and resolves a locale on every call, and
    // the same dates are relabelled on every re-render - a difference-mode
    // toggle, a category toggle, a window step.
    function formatDateLabel(dateKey) {
        let label = dateLabels.get(dateKey);
        if (label === undefined) {
            label = Data.parseDateKey(dateKey).toLocaleDateString("en-SE", {
                weekday: "short", day: "numeric", month: "short", timeZone: "UTC"
            });
            dateLabels.set(dateKey, label);
        }
        return label;
    }

    function makeCheckboxList(items, selected) {
        return items.map((item) => `<label><input type="checkbox" value="${escapeHtml(item.code)}"${selected.has(item.code) ? " checked" : ""}> ${escapeHtml(item.shortName || item.name)}</label>`).join("");
    }

    function setFreshness(payload) {
        const approximate = payload?.inventoryQuality === "approximated-current";
        elements.freshness.classList.toggle("is-stale", Boolean(payload?.stale || approximate));
        let next;
        if (!payload?.dataAsOf) {
            next = "<strong>Supplement unavailable</strong><span>No snapshot has been published.</span>";
        } else if (payload.stale) {
            next = `<strong>Data is stale</strong><span>Last published snapshot: ${escapeHtml(payload.dataAsOf)}.</span>`;
        } else if (approximate) {
            next = `<strong>Historical inventory is approximate</strong><span>Exact inventory history begins ${escapeHtml(payload.inventoryExactFrom)}; booking lifecycle facts remain historical.</span>`;
        } else {
            next = `<strong>Published through ${escapeHtml(payload.dataAsOf)}</strong>`;
        }
        // The chip is a role="status" region and this runs on every grid load, so every branch
        // goes through one guarded write. Rewriting identical markup would announce a change
        // that did not happen.
        if (elements.freshness.innerHTML !== next) elements.freshness.innerHTML = next;
    }

    function showUnavailable(error) {
        state.payload = null;
        elements.tableMount.innerHTML = `<div class="supplement-empty"><strong>Supplement data unavailable</strong><span>${escapeHtml(error.message || "No published data is available.")}</span></div>`;
        elements.freshness.classList.add("is-stale");
        elements.freshness.innerHTML = "<strong>Live data unavailable</strong><span>The last published snapshot could not be loaded.</span>";
        elements.rangeSummary.textContent = "No live data loaded";
        elements.dateWindowNav.hidden = true;
    }

    function categoriesForHotel() {
        return state.categoriesByHotel[state.hotelCode] || [];
    }

    function rebuildCategoryControls(resetSelection) {
        const categories = categoriesForHotel();
        if (resetSelection) state.enabledCategories = new Set(categories.map(({ code }) => code));
        elements.categoryOptions.innerHTML = makeCheckboxList(categories, state.enabledCategories);
        elements.categorySelectionSummary.textContent = state.enabledCategories.size === categories.length
            ? `All ${categories.length}` : `${state.enabledCategories.size} of ${categories.length}`;
    }

    async function initialize() {
        elements.startDate.value = state.startDate;
        elements.endDate.value = state.endDate;
        // The metadata and the first grid are independent queries, and waiting
        // for one before starting the other put a whole extra round trip in
        // front of the first thing anyone sees. With no hotel named, the grid
        // endpoint falls back to the same first hotel and all of its categories
        // that the metadata is about to select, so the speculative request is
        // the request - it is only discarded if that assumption turns out not
        // to hold. Failures are captured rather than thrown so an unavailable
        // grid cannot surface as an unhandled rejection while metadata is still
        // in flight.
        const speculativeGrid = Data
            .fetchGrid(gridParameters({ hotelCodes: [] }), API_BASE_URL)
            .then((payload) => ({ payload }), (error) => ({ error }));

        try {
            const metadata = await Data.fetchMetadata(API_BASE_URL);
            state.hotels = metadata.hotels || [];
            state.categoriesByHotel = metadata.categoriesByHotel || {};
            if (!state.hotels.length) throw new Error("No Supplement hotels have been published.");
            state.hotelCode = state.hotels[0].code;
            state.enabledHotels = new Set(state.hotels.map(({ code }) => code));
            elements.hotel.innerHTML = state.hotels.map((hotel) => `<option value="${escapeHtml(hotel.code)}">${escapeHtml(hotel.name)}</option>`).join("");
            elements.hotelVisibilityOptions.innerHTML = makeCheckboxList(state.hotels, state.enabledHotels);
            elements.hotelSelectionSummary.textContent = `All ${state.hotels.length}`;
            rebuildCategoryControls(true);
            setFreshness(metadata);

            const settled = await speculativeGrid;
            if (settled.error) {
                // The real request would carry the same dates and bases, so it
                // would fail the same way. Report it rather than repeat it.
                showUnavailable(settled.error);
                return;
            }
            const servedHotel = settled.payload?.parameters?.hotelCodes?.[0];
            await loadGrid(servedHotel === state.hotelCode ? settled.payload : null);
        } catch (error) {
            showUnavailable(error);
        }
    }

    function columnsForDate(date) {
        if (date.isPast) {
            const columns = [{ mode: "today", label: "OTB" }, { mode: "ly", label: "LY" }];
            if (state.pastLyDiff) columns.push({ mode: "lyDiff", label: "LY Δ", comparison: "ly" });
            return columns;
        }
        const columns = [{ mode: "today", label: "OTB" }, { mode: "spit", label: "SPIT" }];
        if (state.futureSpitDiff) columns.push({ mode: "spitDiff", label: "SPIT Δ", comparison: "spit" });
        columns.push({ mode: "ly", label: "LY" });
        if (state.futureLyDiff) columns.push({ mode: "lyDiff", label: "LY Δ", comparison: "ly" });
        return columns;
    }

    function differenceValue(cell, comparison, metric) {
        return Data.calculateDifference(cell.today?.[metric], cell[comparison]?.[metric], state.differenceMode);
    }

    // The button's identity depends on the row and the date but not the metric,
    // and it carries three escapes, so it is built once per cell column rather
    // than once per metric row.
    function detailAttributes(row, date) {
        // A disabled total-row button is pixel-identical to a working one, so point it at the
        // hidden note that says why there is nothing to open behind it.
        if (row.isTotal) return ' disabled aria-describedby="totalRowNote"';
        const detailHotel = state.mode === "comparison" ? row.code : state.hotelCode;
        const detailCategory = state.mode === "single" ? row.code : "";
        return ` data-detail-hotel="${escapeHtml(detailHotel)}" data-detail-category="${escapeHtml(detailCategory)}" data-detail-date="${date.date}"`;
    }

    function renderMetricCell(rowLabel, isTotalRow, detailBase, date, cell, column, metric) {
        const todayClass = date.date === todayKey ? " is-today-column" : "";
        const metricAttribute = isTotalRow
            ? detailBase
            : `${detailBase} data-detail-metric="${metric}"`;
        // The visible figure has to come first in the accessible name. Leading with the metric
        // instead would make the name not start with the label on screen (WCAG 2.5.3) and would
        // bury the number when a screen reader lists the grid's buttons.
        const nameFor = (figure) => ` aria-label="${escapeHtml(`${figure} ${metricLabels[metric]} ${column.label}, ${rowLabel}, ${date.date}`)}"`;
        if (column.comparison) {
            const value = differenceValue(cell, column.comparison, metric);
            const direction = value > 0 ? "is-positive" : value < 0 ? "is-negative" : "";
            const highlight = state.highlightedMetrics.has(metric) ? " is-highlighted" : "";
            const text = Data.formatDifference(value, state.differenceMode, metric);
            return `<td class="metric-difference ${direction}${highlight}${todayClass}"><button type="button"${metricAttribute}${nameFor(text)}>${escapeHtml(text)}</button></td>`;
        }
        const text = Data.formatMetric(cell[column.mode]?.[metric], metric);
        return `<td class="${todayClass.trim()}"><button type="button"${metricAttribute}${nameFor(text)}>${escapeHtml(text)}</button></td>`;
    }

    function visibleWindow(payload) {
        const maximumStart = Math.max(0, payload.dates.length - DATE_WINDOW_SIZE);
        state.windowStart = Math.min(state.windowStart, maximumStart);
        const end = Math.min(payload.dates.length, state.windowStart + DATE_WINDOW_SIZE);
        elements.dateWindowNav.hidden = payload.dates.length <= DATE_WINDOW_SIZE;
        elements.dateWindowLabel.textContent = payload.dates.length
            ? `Days ${state.windowStart + 1}–${end} of ${payload.dates.length}`
            : "";
        elements.previousDateWindow.disabled = state.windowStart === 0;
        elements.nextDateWindow.disabled = end >= payload.dates.length;
        return { dates: payload.dates.slice(state.windowStart, end), start: state.windowStart };
    }

    /**
     * The rows to show, derived from the published grid rather than requested.
     *
     * Every category the hotel has is always fetched, so switching one off is a
     * filter over data already in memory instead of a new round trip. The total
     * row is re-derived from the additive facts in the surviving cells, which is
     * the same weighting the server applies - and when nothing is filtered out,
     * the server's own total row is used unchanged.
     */
    function visibleRows(payload) {
        if (state.mode !== "single") return payload.rows;
        const categoryRows = payload.rows.filter((row) => !row.isTotal);
        const selected = categoryRows.filter((row) => state.enabledCategories.has(row.code));
        if (!selected.length) return [];
        if (selected.length === categoryRows.length) return payload.rows;
        return [...selected, {
            rowType: "category",
            code: "total",
            label: "Selected categories",
            shortLabel: "Total",
            isTotal: true,
            cells: Data.sumRowCells(selected, payload.dates.length)
        }];
    }

    function describeRange(payload) {
        const dayCount = payload.dates.length;
        return `${dayCount}-day view · ${state.inventoryBasis === "sellable" ? "sellable" : "physical"} inventory`
            + ` · ${state.lyComparisonType === "sameWeekday" ? "same weekday LY" : "same date LY"}`;
    }

    // The single place the grid is painted from. It owns the range summary too,
    // because emptying the category selection is now a local change and the
    // summary has to follow it back and forth without a reload.
    function renderGrid() {
        if (!state.payload) return;
        const rows = visibleRows(state.payload);
        elements.rangeSummary.textContent = rows.length
            ? describeRange(state.payload)
            : "No rows selected";
        renderTable(state.payload, rows);
    }

    function renderTable(payload, rows = payload.rows) {
        // Written before the empty-case return below, so a selection that empties the grid is
        // announced too. payload.dates is the whole requested range; the visible slice does not
        // exist until visibleWindow() runs a few lines down.
        elements.gridAnnouncement.textContent = rows.length
            ? `Revenue grid updated: ${rows.length} row${rows.length === 1 ? "" : "s"} across ${payload.dates.length} date${payload.dates.length === 1 ? "" : "s"}.`
            : "Revenue grid is empty. Select at least one room category or hotel.";
        if (!rows.length) {
            elements.tableMount.innerHTML = '<div class="supplement-empty"><strong>No rows selected</strong><span>Select at least one room category or hotel.</span></div>';
            elements.dateWindowNav.hidden = true;
            return;
        }
        const window = visibleWindow(payload);
        // The column layout depends only on the date, but the body asks for it
        // once per row per metric per date - on a full window that was thousands
        // of identical array builds per render.
        const columnsByDate = window.dates.map(columnsForDate);
        const dateHeaders = window.dates.map((date, index) => `<th scope="colgroup" colspan="${columnsByDate[index].length}" class="date-group${date.isWeekend ? " is-weekend" : ""}${date.date === todayKey ? " is-today" : ""}"><span>${escapeHtml(formatDateLabel(date.date))}</span><small>${escapeHtml(date.date)}</small></th>`).join("");
        const modeHeaders = window.dates.map((date, index) => columnsByDate[index].map((column) => `<th scope="col" class="mode-column${column.comparison ? " difference-column" : ""}${date.date === todayKey ? " is-today-column" : ""}">${column.label}</th>`).join("")).join("");
        const body = rows.map((row) => {
            const averages = Data.computeRowAverages(row);
            // Both of these depend on the row and the date but not the metric,
            // so they are built once rather than three times over.
            const rowLabelText = row.shortLabel || row.label;
            const detailAttributesByDate = window.dates.map((date) => detailAttributes(row, date));
            return Data.METRICS.map((metric, metricIndex) => {
                const rowLabel = metricIndex === 0
                    ? `<th class="row-group-label" rowspan="3" scope="rowgroup"><strong>${escapeHtml(rowLabelText)}</strong><span>${escapeHtml(row.label)}</span></th>`
                    : "";
                const cells = window.dates.map((date, localIndex) => columnsByDate[localIndex].map((column) => renderMetricCell(rowLabelText, row.isTotal, detailAttributesByDate[localIndex], date, row.cells[window.start + localIndex], column, metric)).join("")).join("");
                const averageCells = ["today", "spit", "ly"].map((mode) => `<td class="average-cell">${escapeHtml(Data.formatMetric(averages[mode][metric], metric))}</td>`).join("");
                const classes = `${row.isTotal ? " total-metric-row" : ""}${metricIndex === 0 ? " group-start" : ""}`;
                return `<tr class="${classes.trim()}">${rowLabel}<th class="metric-label" scope="row">${metricLabels[metric]}</th>${cells}${averageCells}</tr>`;
            }).join("");
        }).join("");
        elements.tableMount.innerHTML = `<table class="supplement-table"><caption class="visually-hidden">Revenue grid by ${state.mode === "single" ? "room category" : "hotel"}, showing OCC, ADR and RevPAR for each date.</caption><thead><tr><th rowspan="2" class="sticky-label">${state.mode === "single" ? "Category" : "Hotel"}</th><th rowspan="2" class="sticky-metric">Metric</th>${dateHeaders}<th colspan="3" class="average-group">Period average</th></tr><tr>${modeHeaders}<th class="mode-column average-group">OTB</th><th class="mode-column average-group">SPIT</th><th class="mode-column average-group">LY</th></tr></thead><tbody>${body}</tbody></table>`;
        elements.tableMount.classList.remove("is-refreshing");
        void elements.tableMount.offsetWidth;
        elements.tableMount.classList.add("is-refreshing");
    }

    function gridParameters(overrides = {}) {
        return {
            startDate: elements.startDate.value || state.startDate,
            endDate: elements.endDate.value || state.endDate,
            mode: state.mode,
            hotelCodes: state.mode === "single"
                ? [elements.hotel.value || state.hotelCode].filter(Boolean)
                : [...state.enabledHotels],
            // Deliberately never narrowed to the enabled categories: fetching
            // the hotel's full set makes a category toggle a local filter, and
            // it keeps one cache entry per hotel and period instead of one per
            // subset of categories a user happens to tick.
            roomCategories: [],
            lyComparisonBasis: elements.lyBasis.value || state.lyComparisonType,
            inventoryBasis: elements.inventoryBasis.value || state.inventoryBasis,
            ...overrides
        };
    }

    // Requests that only differ in which categories are ticked now collapse onto
    // one key, so going back to a hotel or period already looked at this session
    // repaints without touching the network at all. The lifetime matches the
    // Cache-Control the API sends, so nothing is shown for longer than the API
    // considers it fresh.
    const GRID_CACHE_TTL_MS = 5 * 60 * 1000;
    const GRID_CACHE_LIMIT = 8;
    const gridCache = new Map();

    function cachedGrid(key) {
        const entry = gridCache.get(key);
        if (!entry) return null;
        if (Date.now() - entry.storedAt > GRID_CACHE_TTL_MS) {
            gridCache.delete(key);
            return null;
        }
        return entry.payload;
    }

    function cacheGrid(key, payload) {
        if (gridCache.size >= GRID_CACHE_LIMIT) gridCache.clear();
        gridCache.set(key, { storedAt: Date.now(), payload });
    }

    async function loadGrid(preloadedPayload = null) {
        state.startDate = elements.startDate.value;
        state.endDate = elements.endDate.value;
        state.hotelCode = elements.hotel.value || state.hotelCode;
        // Falling back rather than overwriting, the same way the hotel does:
        // these feed the request key and the cache key, and a control that has
        // not been populated yet must not silently drop the basis from both.
        state.lyComparisonType = elements.lyBasis.value || state.lyComparisonType;
        state.inventoryBasis = elements.inventoryBasis.value || state.inventoryBasis;
        const validation = Data.validateDateRange(state.startDate, state.endDate);
        elements.validation.hidden = validation.valid;
        elements.validation.textContent = validation.error || "";
        if (!validation.valid) return;
        if (state.mode === "comparison" && state.enabledHotels.size === 0) {
            state.payload = { dates: [], rows: [] };
            // This path never reaches renderTable, so it has to announce the empty grid itself.
            elements.gridAnnouncement.textContent = "Revenue grid is empty. Select at least one room category or hotel.";
            elements.tableMount.innerHTML = '<div class="supplement-empty"><strong>No rows selected</strong><span>Select at least one room category or hotel.</span></div>';
            elements.rangeSummary.textContent = "No rows selected";
            elements.dateWindowNav.hidden = true;
            return;
        }
        const parameters = gridParameters();
        const cacheKey = JSON.stringify(parameters);
        const requestId = ++state.requestId;
        const alreadyHave = preloadedPayload || cachedGrid(cacheKey);
        if (!alreadyHave) {
            elements.tableMount.setAttribute("aria-busy", "true");
            elements.rangeSummary.textContent = "Loading published revenue facts…";
        }
        try {
            const payload = alreadyHave
                || await Data.fetchGrid(parameters, API_BASE_URL);
            if (requestId !== state.requestId) return;
            cacheGrid(cacheKey, payload);
            state.payload = payload;
            state.windowStart = 0;
            setFreshness(payload);
            renderGrid();
        } catch (error) {
            if (requestId === state.requestId) showUnavailable(error);
        } finally {
            if (requestId === state.requestId) elements.tableMount.removeAttribute("aria-busy");
        }
    }

    function renderLocalChange() {
        state.differenceMode = elements.diffMode.value;
        state.pastLyDiff = elements.pastLyDiff.checked;
        state.futureSpitDiff = elements.futureSpitDiff.checked;
        state.futureLyDiff = elements.futureLyDiff.checked;
        renderGrid();
    }

    function updateViewMode(mode) {
        state.mode = mode;
        document.querySelectorAll("[data-view-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.viewMode === mode)));
        const comparison = mode === "comparison";
        elements.singleHotelControl.hidden = comparison;
        elements.categoryControl.hidden = comparison;
        elements.hotelVisibilityControl.hidden = !comparison;
        loadGrid();
    }

    function readSelected(container) {
        return new Set(Array.from(container.querySelectorAll('input[type="checkbox"]:checked'), ({ value }) => value));
    }

    function curveSvg(currentPoints, comparisonPoints) {
        const current = [...currentPoints].sort((a, b) => b.daysBeforeStay - a.daysBeforeStay);
        const comparison = [...comparisonPoints].sort((a, b) => b.daysBeforeStay - a.daysBeforeStay);
        const allPoints = [...current, ...comparison];
        if (!allPoints.length) {
            return '<div class="supplement-empty pickup-empty"><strong>No pickup history yet</strong><span>No daily snapshots have been recorded for these dates.</span></div>';
        }

        const width = 880;
        const height = 330;
        const margin = { top: 24, right: 28, bottom: 54, left: 54 };
        const plotWidth = width - margin.left - margin.right;
        const plotHeight = height - margin.top - margin.bottom;
        const maximumDay = Math.max(...allPoints.map(({ daysBeforeStay }) => daysBeforeStay));
        const minimumDay = Math.min(...allPoints.map(({ daysBeforeStay }) => daysBeforeStay));
        const daySpan = Math.max(1, maximumDay - minimumDay);
        const maximumRooms = Math.max(1, ...allPoints.map(({ assignedRooms }) => assignedRooms || 0));
        const roundedMaximum = Math.max(5, Math.ceil(maximumRooms / 5) * 5);
        const x = (day) => margin.left + (maximumDay - day) / daySpan * plotWidth;
        const y = (rooms) => margin.top + plotHeight - (rooms || 0) / roundedMaximum * plotHeight;
        const pathFor = (points) => points.map((point, index) =>
            `${index ? "L" : "M"}${x(point.daysBeforeStay).toFixed(1)},${y(point.assignedRooms).toFixed(1)}`
        ).join(" ");
        const currentPath = pathFor(current);
        const comparisonPath = pathFor(comparison);
        const baseline = margin.top + plotHeight;
        const areaPath = current.length
            ? `${currentPath} L${x(current.at(-1).daysBeforeStay).toFixed(1)},${baseline} L${x(current[0].daysBeforeStay).toFixed(1)},${baseline} Z`
            : "";

        const yGrid = [0, .25, .5, .75, 1].map((ratio) => {
            const value = roundedMaximum * ratio;
            return `<line x1="${margin.left}" x2="${width - margin.right}" y1="${y(value)}" y2="${y(value)}"></line><text x="${margin.left - 12}" y="${y(value) + 4}">${Math.round(value)}</text>`;
        }).join("");
        // Tick density follows the window: a 7 day view labels every day or two,
        // a two year view lands on round intervals instead of arbitrary numbers
        // like "417d". Ticks are always whole days and never overlap.
        const niceSteps = [1, 2, 5, 7, 10, 14, 30, 60, 90, 180, 365, 730];
        const targetTicks = 6;
        const step = niceSteps.find((candidate) => daySpan / candidate <= targetTicks)
            || Math.ceil(daySpan / targetTicks);
        const xTicks = [];
        for (let day = Math.max(0, minimumDay); day <= maximumDay; day += step) {
            xTicks.push(day);
        }
        if (xTicks[xTicks.length - 1] !== maximumDay) xTicks.push(maximumDay);
        if (minimumDay <= 0 && !xTicks.includes(0)) xTicks.unshift(0);

        const dayLabel = (day) => day === 0 ? "Stay" : day < 0
            ? `+${Math.abs(day)}d` : `${day}d`;
        const xGrid = xTicks.map((day) =>
            `<line class="pickup-x-grid" x1="${x(day)}" x2="${x(day)}" y1="${margin.top}" y2="${baseline}"></line><text class="pickup-x-label" x="${x(day)}" y="${baseline + 25}">${dayLabel(day)}</text>`
        ).join("");
        // The line always uses every point; only the markers thin out, so a long
        // window stays a readable curve rather than a solid band of circles.
        const pointMarks = (points, className) => {
            const markerStep = Math.max(1, Math.ceil(points.length / 24));
            return points.filter((_point, index) => index % markerStep === 0 || index === points.length - 1)
                .map((point) => `<circle class="${className}" cx="${x(point.daysBeforeStay)}" cy="${y(point.assignedRooms)}" r="3"><title>${escapeHtml(point.viewDate)}: ${point.assignedRooms} rooms</title></circle>`).join("");
        };

        return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Rooms on the books by days before stay"><title>Pickup pace from ${maximumDay} to ${minimumDay} days before stay</title><g class="pickup-grid">${yGrid}${xGrid}</g>${areaPath ? `<path class="pickup-current-area" d="${areaPath}"></path>` : ""}${comparisonPath ? `<path class="pickup-comparison-line" d="${comparisonPath}"></path>` : ""}${currentPath ? `<path class="pickup-current-line" d="${currentPath}"></path>` : ""}<g class="pickup-points">${pointMarks(comparison, "pickup-comparison-point")}${pointMarks(current, "pickup-current-point")}</g><text class="pickup-axis-title" x="${margin.left + plotWidth / 2}" y="${height - 8}">Days before stay</text></svg>`;
    }

    // The lookback control. Every whole day is reachable: the slider covers the
    // common range, the number input and steppers reach any value including far
    // beyond the slider's maximum, and "from first booking" removes the bound
    // entirely rather than substituting a large number.
    function syncWindowControls() {
        const all = state.pickupWindowAll;
        elements.pickupWindowAll.checked = all;
        elements.pickupWindowValue.disabled = all;
        elements.pickupWindowSlider.disabled = all;
        elements.pickupWindowUp.disabled = all;
        elements.pickupWindowDown.disabled = all || state.pickupWindowDays <= 1;
        if (!all) {
            elements.pickupWindowValue.value = String(state.pickupWindowDays);
            // Keep the slider usable when the value exceeds its range instead of
            // silently clamping the request.
            elements.pickupWindowSlider.max = String(
                Math.max(365, state.pickupWindowDays)
            );
            elements.pickupWindowSlider.value = String(state.pickupWindowDays);
        }
    }

    function describeWindow(payload) {
        if (!payload) return "";
        const available = payload.pickupHistoryDays || 0;
        if (state.pickupWindowAll) {
            return available
                ? `Showing the full history: ${available} days before the stay date.`
                : "No pickup history for this stay date yet.";
        }
        if (available && state.pickupWindowDays > available) {
            return `History starts ${available} days before the stay date, so that is all this window can show.`;
        }
        return `Showing ${state.pickupWindowDays} days before the stay date, of ${available} available.`;
    }

    function setPickupWindow(days, useAll) {
        const nextAll = Boolean(useAll);
        const nextDays = Math.max(1, Math.round(Number(days) || 1));
        if (nextAll === state.pickupWindowAll && nextDays === state.pickupWindowDays) {
            syncWindowControls();
            return;
        }
        state.pickupWindowAll = nextAll;
        state.pickupWindowDays = nextDays;
        syncWindowControls();
        if (state.pickupRequest) refreshDetail();
    }

    function bindWindowControls() {
        elements.pickupWindowSlider.addEventListener("input", () => {
            setPickupWindow(elements.pickupWindowSlider.value, false);
        });
        elements.pickupWindowValue.addEventListener("change", () => {
            setPickupWindow(elements.pickupWindowValue.value, false);
        });
        elements.pickupWindowUp.addEventListener("click", () => {
            setPickupWindow(state.pickupWindowDays + 1, false);
        });
        elements.pickupWindowDown.addEventListener("click", () => {
            setPickupWindow(state.pickupWindowDays - 1, false);
        });
        elements.pickupWindowAll.addEventListener("change", () => {
            setPickupWindow(state.pickupWindowDays, elements.pickupWindowAll.checked);
        });
    }

    async function refreshDetail() {
        if (!state.pickupRequest) return;
        elements.pickupCurve.innerHTML = '<div class="pickup-loading" role="progressbar" aria-label="Loading pickup pace"></div>';
        // The dialog is already open here, so nothing else announces the reload; #pickupCoverage
        // is the polite region that will carry the new coverage line once the fetch settles.
        elements.pickupCoverage.textContent = "Loading pickup pace…";
        try {
            const payload = await Data.fetchDetail({
                ...state.pickupRequest,
                daysBeforeStay: state.pickupWindowAll ? "all" : state.pickupWindowDays
            }, API_BASE_URL);
            renderPickup(payload);
        }
        catch (error) {
            elements.pickupCurve.innerHTML = "";
            elements.pickupCoverage.textContent =
                `Could not reload the pickup curve: ${error.message}`;
        }
    }

    function renderPickup(payload) {
        elements.pickupCurve.innerHTML = curveSvg(payload.pickup, payload.comparisonPickup);
        const currentLast = payload.pickup.at(-1);
        const comparisonLast = payload.comparisonPickup.at(-1);
        elements.pickupCurrentLabel.textContent = currentLast
            ? `Current · ${currentLast.assignedRooms} rooms` : "Current · no history";
        elements.pickupComparisonLabel.textContent = comparisonLast
            ? `${payload.comparison} · ${comparisonLast.assignedRooms} rooms`
            : payload.comparisonAvailable
                ? `${payload.comparison} · no history`
                : `${payload.comparison} · no history yet`;
        const coverageParts = [];
        if (payload.pickup.length) {
            coverageParts.push(`Current: ${payload.pickup.length} day${payload.pickup.length === 1 ? "" : "s"}`);
        }
        if (payload.comparisonPickup.length) {
            coverageParts.push(`${payload.comparison}: ${payload.comparisonPickup.length} day${payload.comparisonPickup.length === 1 ? "" : "s"}`);
        }
        if (!payload.comparisonAvailable) {
            coverageParts.push(`${payload.comparison} comparison is not available yet`);
        }
        elements.pickupCoverage.textContent = coverageParts.join(" · ");
        elements.pickupWindowHint.textContent = describeWindow(payload);
    }

    async function openDetail(button) {
        const metric = button.dataset.detailMetric;
        elements.detailTitle.textContent = `${metricLabels[metric]} detail`;
        elements.detailContext.textContent = "Loading published detail…";
        elements.detailError.hidden = true;
        elements.dialog.classList.remove("has-error");
        elements.dialog.setAttribute("aria-busy", "true");
        elements.detailRooms.textContent = "—";
        elements.detailAdr.textContent = "—";
        elements.detailInventory.textContent = "—";
        elements.detailOccupancy.textContent = "—";
        elements.detailBreakdown.innerHTML = "";
        elements.pickupCurve.innerHTML = '<div class="pickup-loading" role="progressbar" aria-label="Loading pickup pace"></div>';
        elements.pickupCoverage.textContent = "";
        elements.pickupCurrentLabel.textContent = "Current · loading";
        elements.pickupComparisonLabel.textContent = "Comparison · loading";
        // A blank footnote that fills in a moment later reads as a layout jump, so hold the row.
        elements.dialogFootnote.textContent = "Loading…";
        elements.dialog.showModal();
        try {
            state.pickupRequest = {
                hotelCode: button.dataset.detailHotel,
                stayDate: button.dataset.detailDate,
                roomCategory: button.dataset.detailCategory || "",
                lyComparisonBasis: state.lyComparisonType,
                inventoryBasis: state.inventoryBasis
            };
            syncWindowControls();
            const payload = await Data.fetchDetail({
                ...state.pickupRequest,
                daysBeforeStay: state.pickupWindowAll ? "all" : state.pickupWindowDays
            }, API_BASE_URL);
            const hotelName = state.hotels.find(({ code }) => code === payload.hotelCode)?.name || payload.hotelCode;
            const categoryName = (state.categoriesByHotel[payload.hotelCode] || [])
                .find(({ code }) => code === payload.roomCategory)?.name;
            // Clear aria-busy before the first success write, not in the finally block. A polite
            // update inside an aria-busy subtree is withheld, so #detailContext would otherwise
            // never be announced — the attribute would only come off after the text had settled.
            elements.dialog.removeAttribute("aria-busy");
            elements.detailContext.textContent = `${hotelName} · ${categoryName || "All categories"} · ${formatDateLabel(payload.stayDate)} · ${payload.comparison} comparison`;
            const occupancy = payload.inventory > 0
                ? payload.totalAssignedRooms / payload.inventory * 100 : null;
            elements.detailRooms.textContent = new Intl.NumberFormat("en-SE", {
                maximumFractionDigits: 0
            }).format(payload.totalAssignedRooms || 0);
            elements.detailAdr.textContent = payload.totalAveragePrice == null
                ? "—" : `${Data.formatMetric(payload.totalAveragePrice, "adr")} kr`;
            elements.detailInventoryLabel.textContent = payload.inventoryBasis === "physical"
                ? "Physical inventory" : "Sellable inventory";
            elements.detailInventory.textContent = new Intl.NumberFormat("en-SE", {
                maximumFractionDigits: 0
            }).format(payload.inventory || 0);
            elements.detailOccupancy.textContent = Data.formatMetric(occupancy, "occ");
            elements.detailBreakdown.innerHTML = payload.breakdown.length
                ? payload.breakdown.map((row) => `<tr><td>${escapeHtml(row.requestedRoomName)}</td><td>${Data.formatMetric(row.assignedRooms, "adr")}</td><td>${Data.formatMetric(row.averagePrice, "adr")}</td><td>${payload.comparisonAvailable ? Data.formatMetric(row.comparisonAssignedRooms, "adr") : "—"}</td><td>${payload.comparisonAvailable ? Data.formatMetric(row.comparisonAveragePrice, "adr") : "—"}</td></tr>`).join("")
                : '<tr><td colspan="5">No assigned rooms for this stay date.</td></tr>';
            renderPickup(payload);
            elements.dialogFootnote.textContent = payload.inventoryQuality === "approximated-current"
                ? `Published through ${payload.dataAsOf} · inventory is approximated from current rooms before ${payload.inventoryExactFrom}.`
                : `Data through ${payload.dataAsOf}`;
        } catch (error) {
            elements.dialog.classList.add("has-error");
            elements.detailContext.textContent = "The selected metric could not be opened.";
            elements.detailError.textContent = error.message || "Detail data is unavailable.";
            elements.detailError.hidden = false;
            elements.dialogFootnote.textContent = "Close and try again in a moment.";
        } finally {
            elements.dialog.removeAttribute("aria-busy");
        }
    }

    function closeDetailDialog() {
        if (!elements.dialog.open) return;
        if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
            elements.dialog.close();
            return;
        }
        elements.dialog.classList.add("is-closing");
        elements.dialog.addEventListener("animationend", () => {
            elements.dialog.close();
            elements.dialog.classList.remove("is-closing");
        }, { once: true });
    }

    document.querySelectorAll("[data-view-mode]").forEach((button) => button.addEventListener("click", () => updateViewMode(button.dataset.viewMode)));
    elements.hotel.addEventListener("change", () => {
        state.hotelCode = elements.hotel.value;
        rebuildCategoryControls(true);
        loadGrid();
    });
    elements.startDate.addEventListener("change", loadGrid);
    elements.endDate.addEventListener("change", loadGrid);
    elements.lyBasis.addEventListener("change", loadGrid);
    elements.inventoryBasis.addEventListener("change", loadGrid);
    elements.diffMode.addEventListener("change", renderLocalChange);
    elements.pastLyDiff.addEventListener("change", renderLocalChange);
    elements.futureSpitDiff.addEventListener("change", renderLocalChange);
    elements.futureLyDiff.addEventListener("change", renderLocalChange);
    elements.categoryOptions.addEventListener("change", () => {
        state.enabledCategories = readSelected(elements.categoryOptions);
        rebuildCategoryControls(false);
        // Every category is already in state.payload, so this is a repaint, not
        // a request.
        renderGrid();
    });
    elements.hotelVisibilityOptions.addEventListener("change", () => {
        state.enabledHotels = readSelected(elements.hotelVisibilityOptions);
        elements.hotelSelectionSummary.textContent = state.enabledHotels.size === state.hotels.length
            ? `All ${state.hotels.length}` : `${state.enabledHotels.size} of ${state.hotels.length}`;
        loadGrid();
    });
    elements.highlights.addEventListener("change", () => { state.highlightedMetrics = readSelected(elements.highlights); renderLocalChange(); });
    elements.previousDateWindow.addEventListener("click", () => { state.windowStart = Math.max(0, state.windowStart - DATE_WINDOW_SIZE); renderGrid(); });
    elements.nextDateWindow.addEventListener("click", () => { state.windowStart += DATE_WINDOW_SIZE; renderGrid(); });
    elements.tableMount.addEventListener("click", (event) => {
        const button = event.target.closest("button[data-detail-metric]");
        if (button) openDetail(button);
    });
    elements.closeDialog.addEventListener("click", closeDetailDialog);
    elements.dialog.addEventListener("cancel", (event) => {
        event.preventDefault();
        closeDetailDialog();
    });
    elements.dialog.addEventListener("click", (event) => {
        if (event.target === elements.dialog) closeDetailDialog();
    });

    bindWindowControls();
    syncWindowControls();
    initialize();
}());
