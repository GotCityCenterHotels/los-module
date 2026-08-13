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
        differenceMode: "percent",
        pastLyDiff: true,
        futureSpitDiff: false,
        futureLyDiff: false,
        requestId: 0,
        payload: null,
        windowStart: 0
    };

    function escapeHtml(value) {
        return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
    }

    function formatDateLabel(dateKey) {
        return Data.parseDateKey(dateKey).toLocaleDateString("en-SE", {
            weekday: "short", day: "numeric", month: "short", timeZone: "UTC"
        });
    }

    function makeCheckboxList(items, selected) {
        return items.map((item) => `<label><input type="checkbox" value="${escapeHtml(item.code)}"${selected.has(item.code) ? " checked" : ""}> ${escapeHtml(item.shortName || item.name)}</label>`).join("");
    }

    function setFreshness(payload) {
        const approximate = payload?.inventoryQuality === "approximated-current";
        elements.freshness.classList.toggle("is-stale", Boolean(payload?.stale || approximate));
        if (!payload?.dataAsOf) {
            elements.freshness.innerHTML = "<strong>Supplement unavailable</strong><span>No PostgreSQL snapshot has been published.</span>";
            return;
        }
        if (payload.stale) {
            elements.freshness.innerHTML = `<strong>Data is stale</strong><span>Last published snapshot: ${escapeHtml(payload.dataAsOf)}.</span>`;
        } else if (approximate) {
            elements.freshness.innerHTML = `<strong>Historical inventory is approximate</strong><span>Exact inventory history begins ${escapeHtml(payload.inventoryExactFrom)}; booking lifecycle facts remain historical.</span>`;
        } else {
            elements.freshness.innerHTML = `<strong>Published through ${escapeHtml(payload.dataAsOf)}</strong><span>Served from PostgreSQL; integration_db is not queried by this view.</span>`;
        }
    }

    function showUnavailable(error) {
        state.payload = null;
        elements.tableMount.innerHTML = `<div class="supplement-empty"><strong>Supplement data unavailable</strong><span>${escapeHtml(error.message || "No published data is available.")}</span></div>`;
        elements.freshness.classList.add("is-stale");
        elements.freshness.innerHTML = "<strong>Live data unavailable</strong><span>The last published PostgreSQL snapshot could not be loaded.</span>";
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
            await loadGrid();
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

    function renderMetricCell(row, date, cell, column, metric) {
        const detailHotel = state.mode === "comparison" ? row.code : state.hotelCode;
        const detailCategory = state.mode === "single" && !row.isTotal ? row.code : "";
        const detailAttributes = row.isTotal ? " disabled" : ` data-detail-hotel="${escapeHtml(detailHotel)}" data-detail-category="${escapeHtml(detailCategory)}" data-detail-date="${date.date}" data-detail-metric="${metric}"`;
        const todayClass = date.date === todayKey ? " is-today-column" : "";
        if (column.comparison) {
            const value = differenceValue(cell, column.comparison, metric);
            const direction = value > 0 ? "is-positive" : value < 0 ? "is-negative" : "";
            const highlight = state.highlightedMetrics.has(metric) ? " is-highlighted" : "";
            return `<td class="metric-difference ${direction}${highlight}${todayClass}"><button type="button"${detailAttributes}>${escapeHtml(Data.formatDifference(value, state.differenceMode, metric))}</button></td>`;
        }
        return `<td class="${todayClass.trim()}"><button type="button"${detailAttributes}>${escapeHtml(Data.formatMetric(cell[column.mode]?.[metric], metric))}</button></td>`;
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

    function renderTable(payload) {
        if (!payload.rows.length) {
            elements.tableMount.innerHTML = '<div class="supplement-empty"><strong>No rows selected</strong><span>Select at least one room category or hotel.</span></div>';
            return;
        }
        const window = visibleWindow(payload);
        const dateHeaders = window.dates.map((date) => `<th colspan="${columnsForDate(date).length}" class="date-group${date.isWeekend ? " is-weekend" : ""}${date.date === todayKey ? " is-today" : ""}"><span>${escapeHtml(formatDateLabel(date.date))}</span><small>${escapeHtml(date.date)}</small></th>`).join("");
        const modeHeaders = window.dates.map((date) => columnsForDate(date).map((column) => `<th class="mode-column${column.comparison ? " difference-column" : ""}${date.date === todayKey ? " is-today-column" : ""}">${column.label}</th>`).join("")).join("");
        const body = payload.rows.map((row) => {
            const averages = Data.computeRowAverages(row);
            return Data.METRICS.map((metric, metricIndex) => {
                const rowLabel = metricIndex === 0
                    ? `<th class="row-group-label" rowspan="3" scope="rowgroup"><strong>${escapeHtml(row.shortLabel || row.label)}</strong><span>${escapeHtml(row.label)}</span></th>`
                    : "";
                const cells = window.dates.map((date, localIndex) => columnsForDate(date).map((column) => renderMetricCell(row, date, row.cells[window.start + localIndex], column, metric)).join("")).join("");
                const averageCells = ["today", "spit", "ly"].map((mode) => `<td class="average-cell">${escapeHtml(Data.formatMetric(averages[mode][metric], metric))}</td>`).join("");
                const classes = `${row.isTotal ? " total-metric-row" : ""}${metricIndex === 0 ? " group-start" : ""}`;
                return `<tr class="${classes.trim()}">${rowLabel}<th class="metric-label" scope="row">${metricLabels[metric]}</th>${cells}${averageCells}</tr>`;
            }).join("");
        }).join("");
        elements.tableMount.innerHTML = `<table class="supplement-table"><thead><tr><th rowspan="2" class="sticky-label">${state.mode === "single" ? "Category" : "Hotel"}</th><th rowspan="2" class="sticky-metric">Metric</th>${dateHeaders}<th colspan="3" class="average-group">Period average</th></tr><tr>${modeHeaders}<th class="mode-column average-group">OTB</th><th class="mode-column average-group">SPIT</th><th class="mode-column average-group">LY</th></tr></thead><tbody>${body}</tbody></table>`;
        elements.tableMount.classList.remove("is-refreshing");
        void elements.tableMount.offsetWidth;
        elements.tableMount.classList.add("is-refreshing");
    }

    async function loadGrid() {
        state.startDate = elements.startDate.value;
        state.endDate = elements.endDate.value;
        state.hotelCode = elements.hotel.value || state.hotelCode;
        state.lyComparisonType = elements.lyBasis.value;
        state.inventoryBasis = elements.inventoryBasis.value;
        const validation = Data.validateDateRange(state.startDate, state.endDate);
        elements.validation.hidden = validation.valid;
        elements.validation.textContent = validation.error || "";
        if (!validation.valid) return;
        if ((state.mode === "single" && state.enabledCategories.size === 0)
                || (state.mode === "comparison" && state.enabledHotels.size === 0)) {
            state.payload = { dates: [], rows: [] };
            elements.tableMount.innerHTML = '<div class="supplement-empty"><strong>No rows selected</strong><span>Select at least one room category or hotel.</span></div>';
            elements.rangeSummary.textContent = "No rows selected";
            elements.dateWindowNav.hidden = true;
            return;
        }
        const requestId = ++state.requestId;
        elements.tableMount.setAttribute("aria-busy", "true");
        elements.rangeSummary.textContent = "Loading published revenue facts…";
        try {
            const hotelCodes = state.mode === "single" ? [state.hotelCode] : [...state.enabledHotels];
            const payload = await Data.fetchGrid({
                startDate: state.startDate,
                endDate: state.endDate,
                mode: state.mode,
                hotelCodes,
                roomCategories: state.mode === "single" ? [...state.enabledCategories] : [],
                lyComparisonBasis: state.lyComparisonType,
                inventoryBasis: state.inventoryBasis
            }, API_BASE_URL);
            if (requestId !== state.requestId) return;
            state.payload = payload;
            state.windowStart = 0;
            setFreshness(payload);
            elements.rangeSummary.textContent = `${validation.dayCount}-day view · ${state.inventoryBasis === "sellable" ? "sellable" : "physical"} inventory · ${state.lyComparisonType === "sameWeekday" ? "same weekday LY" : "same date LY"}`;
            renderTable(payload);
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
        if (state.payload) renderTable(state.payload);
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
            return '<div class="supplement-empty pickup-empty"><strong>No pickup history yet</strong><span>Backfilled daily snapshots will build this curve over time.</span></div>';
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
        const xTicks = [...new Set([0, .25, .5, .75, 1].map((ratio) =>
            Math.round(maximumDay - daySpan * ratio)
        ))];
        const dayLabel = (day) => day === 0 ? "Stay" : day < 0
            ? `+${Math.abs(day)}d` : `${day}d`;
        const xGrid = xTicks.map((day) =>
            `<line class="pickup-x-grid" x1="${x(day)}" x2="${x(day)}" y1="${margin.top}" y2="${baseline}"></line><text class="pickup-x-label" x="${x(day)}" y="${baseline + 25}">${dayLabel(day)}</text>`
        ).join("");
        const pointMarks = (points, className) => {
            const step = Math.max(1, Math.ceil(points.length / 24));
            return points.filter((_point, index) => index % step === 0 || index === points.length - 1)
                .map((point) => `<circle class="${className}" cx="${x(point.daysBeforeStay)}" cy="${y(point.assignedRooms)}" r="3"><title>${escapeHtml(point.viewDate)}: ${point.assignedRooms} rooms</title></circle>`).join("");
        };

        return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Rooms on the books by days before stay"><title>Pickup pace from ${maximumDay} to ${minimumDay} days before stay</title><g class="pickup-grid">${yGrid}${xGrid}</g>${areaPath ? `<path class="pickup-current-area" d="${areaPath}"></path>` : ""}${comparisonPath ? `<path class="pickup-comparison-line" d="${comparisonPath}"></path>` : ""}${currentPath ? `<path class="pickup-current-line" d="${currentPath}"></path>` : ""}<g class="pickup-points">${pointMarks(comparison, "pickup-comparison-point")}${pointMarks(current, "pickup-current-point")}</g><text class="pickup-axis-title" x="${margin.left + plotWidth / 2}" y="${height - 8}">Days before stay</text></svg>`;
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
        elements.pickupCurve.innerHTML = '<div class="pickup-loading" aria-hidden="true"></div>';
        elements.pickupCoverage.textContent = "";
        elements.pickupCurrentLabel.textContent = "Current · loading";
        elements.pickupComparisonLabel.textContent = "Comparison · loading";
        elements.dialogFootnote.textContent = "Loading from the latest published PostgreSQL snapshot.";
        elements.dialog.showModal();
        try {
            const payload = await Data.fetchDetail({
                hotelCode: button.dataset.detailHotel,
                stayDate: button.dataset.detailDate,
                roomCategory: button.dataset.detailCategory || "",
                lyComparisonBasis: state.lyComparisonType,
                inventoryBasis: state.inventoryBasis
            }, API_BASE_URL);
            const hotelName = state.hotels.find(({ code }) => code === payload.hotelCode)?.name || payload.hotelCode;
            const categoryName = (state.categoriesByHotel[payload.hotelCode] || [])
                .find(({ code }) => code === payload.roomCategory)?.name;
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
            elements.pickupCurve.innerHTML = curveSvg(payload.pickup, payload.comparisonPickup);
            const currentLast = payload.pickup.at(-1);
            const comparisonLast = payload.comparisonPickup.at(-1);
            elements.pickupCurrentLabel.textContent = currentLast
                ? `Current · ${currentLast.assignedRooms} rooms` : "Current · no history";
            elements.pickupComparisonLabel.textContent = comparisonLast
                ? `${payload.comparison} · ${comparisonLast.assignedRooms} rooms`
                : payload.comparisonAvailable
                    ? `${payload.comparison} · no history`
                    : `${payload.comparison} · backfill needed`;
            const coverageParts = [];
            if (payload.pickup.length) {
                coverageParts.push(`Current: ${payload.pickup.length} snapshot${payload.pickup.length === 1 ? "" : "s"}`);
            }
            if (payload.comparisonPickup.length) {
                coverageParts.push(`${payload.comparison}: ${payload.comparisonPickup.length} snapshot${payload.comparisonPickup.length === 1 ? "" : "s"}`);
            }
            if (!payload.comparisonAvailable) {
                coverageParts.push(`${payload.comparison} comparison is not backfilled yet`);
            }
            elements.pickupCoverage.textContent = coverageParts.length
                ? coverageParts.join(" · ")
                : "Pickup coverage will appear as daily snapshots are backfilled.";
            elements.dialogFootnote.textContent = payload.inventoryQuality === "approximated-current"
                ? `Published through ${payload.dataAsOf} · inventory is approximated from current rooms before ${payload.inventoryExactFrom}.`
                : `Published through ${payload.dataAsOf} · served from PostgreSQL.`;
        } catch (error) {
            elements.dialog.classList.add("has-error");
            elements.detailContext.textContent = "The selected metric could not be opened.";
            elements.detailError.textContent = error.message || "Detail data is unavailable.";
            elements.detailError.hidden = false;
            elements.dialogFootnote.textContent = "No simulated fallback was used.";
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
        loadGrid();
    });
    elements.hotelVisibilityOptions.addEventListener("change", () => {
        state.enabledHotels = readSelected(elements.hotelVisibilityOptions);
        elements.hotelSelectionSummary.textContent = state.enabledHotels.size === state.hotels.length
            ? `All ${state.hotels.length}` : `${state.enabledHotels.size} of ${state.hotels.length}`;
        loadGrid();
    });
    elements.highlights.addEventListener("change", () => { state.highlightedMetrics = readSelected(elements.highlights); renderLocalChange(); });
    elements.previousDateWindow.addEventListener("click", () => { state.windowStart = Math.max(0, state.windowStart - DATE_WINDOW_SIZE); renderTable(state.payload); });
    elements.nextDateWindow.addEventListener("click", () => { state.windowStart += DATE_WINDOW_SIZE; renderTable(state.payload); });
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

    initialize();
}());
