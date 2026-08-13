(function () {
    "use strict";

    const Data = window.SupplementData;
    const metricLabels = { occ: "OCC", adr: "ADR", revpar: "RevPAR" };
    const modeLabels = { today: "OTB", ly: "LY", spit: "SPIT" };
    const elements = {
        hotel: document.getElementById("supplementHotel"),
        singleHotelControl: document.getElementById("singleHotelControl"),
        categoryControl: document.getElementById("categoryControl"),
        hotelVisibilityControl: document.getElementById("hotelVisibilityControl"),
        categoryOptions: document.getElementById("categoryOptions"),
        hotelVisibilityOptions: document.getElementById("hotelVisibilityOptions"),
        startDate: document.getElementById("supplementStartDate"),
        endDate: document.getElementById("supplementEndDate"),
        lyBasis: document.getElementById("supplementLyBasis"),
        diffMode: document.getElementById("supplementDiffMode"),
        highlights: document.getElementById("metricHighlightOptions"),
        pastLyDiff: document.getElementById("pastLyDiff"),
        futureSpitDiff: document.getElementById("futureSpitDiff"),
        futureLyDiff: document.getElementById("futureLyDiff"),
        validation: document.getElementById("supplementValidation"),
        tableMount: document.getElementById("supplementTableMount"),
        rangeSummary: document.getElementById("rangeSummary"),
        dialog: document.getElementById("supplementDetailDialog"),
        detailTitle: document.getElementById("detailTitle"),
        detailContext: document.getElementById("detailContext"),
        detailBreakdown: document.getElementById("detailBreakdown"),
        pickupCurve: document.getElementById("pickupCurve"),
        closeDialog: document.getElementById("closeDetailDialog")
    };

    const today = new Date();
    const todayUtc = new Date(Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()));
    const state = {
        mode: "single",
        hotelCode: Data.HOTELS[0].code,
        enabledCategories: new Set(Data.CATEGORIES.map(({ code }) => code)),
        enabledHotels: new Set(Data.HOTELS.map(({ code }) => code)),
        highlightedMetrics: new Set(["occ"]),
        startDate: Data.formatDateKey(new Date(todayUtc.getTime() - 3 * 86_400_000)),
        endDate: Data.formatDateKey(new Date(todayUtc.getTime() + 3 * 86_400_000)),
        lyComparisonType: "sameDate",
        differenceMode: "percent",
        pastLyDiff: true,
        futureSpitDiff: false,
        futureLyDiff: false
    };

    function escapeHtml(value) {
        return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
    }

    function formatDateLabel(dateKey) {
        const date = Data.parseDateKey(dateKey);
        return date.toLocaleDateString("en-SE", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
    }

    function makeCheckboxList(items, selected) {
        return items.map((item) => `<label><input type="checkbox" value="${escapeHtml(item.code)}"${selected.has(item.code) ? " checked" : ""}> ${escapeHtml(item.shortName || item.name)}</label>`).join("");
    }

    function initializeControls() {
        elements.hotel.innerHTML = Data.HOTELS.map((hotel) => `<option value="${hotel.code}">${escapeHtml(hotel.name)}</option>`).join("");
        elements.categoryOptions.innerHTML = makeCheckboxList(Data.CATEGORIES, state.enabledCategories);
        elements.hotelVisibilityOptions.innerHTML = makeCheckboxList(Data.HOTELS, state.enabledHotels);
        elements.startDate.value = state.startDate;
        elements.endDate.value = state.endDate;
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
        return Data.calculateDifference(
            cell.today.metrics[metric],
            cell[comparison].metrics[metric],
            state.differenceMode
        );
    }

    function renderMetricCell(row, date, cell, column, metric) {
        const detailHotel = state.mode === "comparison" ? row.code : state.hotelCode;
        const detailCategory = state.mode === "single" && !row.isTotal ? row.code : "";
        const detailAttributes = row.isTotal ? " disabled" : ` data-detail-hotel="${detailHotel}" data-detail-category="${detailCategory}" data-detail-date="${date.date}" data-detail-metric="${metric}"`;
        if (column.comparison) {
            const value = differenceValue(cell, column.comparison, metric);
            const direction = value > 0 ? "is-positive" : value < 0 ? "is-negative" : "";
            const highlight = state.highlightedMetrics.has(metric) ? " is-highlighted" : "";
            return `<td class="metric-difference ${direction}${highlight}"><button type="button"${detailAttributes}>${escapeHtml(Data.formatDifference(value, state.differenceMode, metric))}</button></td>`;
        }
        const value = cell[column.mode].metrics[metric];
        return `<td><button type="button"${detailAttributes}>${escapeHtml(Data.formatMetric(value, metric))}</button></td>`;
    }

    function renderTable(dataset, rows) {
        if (!rows.length) {
            elements.tableMount.innerHTML = '<div class="supplement-empty"><strong>No rows selected</strong><span>Select at least one room category or hotel to populate the preview.</span></div>';
            return;
        }

        const dateHeaders = dataset.dates.map((date) => {
            const columns = columnsForDate(date);
            return `<th colspan="${columns.length}" class="date-group${date.isWeekend ? " is-weekend" : ""}"><span>${escapeHtml(formatDateLabel(date.date))}</span><small>${escapeHtml(date.date)}</small></th>`;
        }).join("");
        const modeHeaders = dataset.dates.map((date) => columnsForDate(date)
            .map((column) => `<th class="mode-column${column.comparison ? " difference-column" : ""}">${column.label}</th>`).join("")).join("");

        const body = rows.map((row) => {
            const averages = Data.computeRowAverages(row);
            return Data.METRICS.map((metric, metricIndex) => {
                const rowLabel = metricIndex === 0
                    ? `<th class="row-group-label" rowspan="3" scope="rowgroup"><strong>${escapeHtml(row.shortLabel || row.label)}</strong><span>${escapeHtml(row.label)}</span></th>`
                    : "";
                const cells = dataset.dates.map((date, index) => columnsForDate(date)
                    .map((column) => renderMetricCell(row, date, row.cells[index], column, metric)).join("")).join("");
                const averageCells = ["today", "spit", "ly"].map((mode) => `<td class="average-cell">${escapeHtml(Data.formatMetric(averages[mode][metric], metric))}</td>`).join("");
                const classes = `${row.isTotal ? " total-metric-row" : ""}${metricIndex === 0 ? " group-start" : ""}`;
                return `<tr class="${classes.trim()}">${rowLabel}<th class="metric-label" scope="row">${metricLabels[metric]}</th>${cells}${averageCells}</tr>`;
            }).join("");
        }).join("");

        elements.tableMount.innerHTML = `<table class="supplement-table">
            <thead>
                <tr><th rowspan="2" class="sticky-label">${state.mode === "single" ? "Category" : "Hotel"}</th><th rowspan="2" class="sticky-metric">Metric</th>${dateHeaders}<th colspan="3" class="average-group">Period average</th></tr>
                <tr>${modeHeaders}<th class="mode-column average-group">OTB</th><th class="mode-column average-group">SPIT</th><th class="mode-column average-group">LY</th></tr>
            </thead>
            <tbody>${body}</tbody>
        </table>`;
        elements.tableMount.classList.remove("is-refreshing");
        void elements.tableMount.offsetWidth;
        elements.tableMount.classList.add("is-refreshing");
    }

    function render() {
        state.startDate = elements.startDate.value;
        state.endDate = elements.endDate.value;
        state.hotelCode = elements.hotel.value;
        state.lyComparisonType = elements.lyBasis.value;
        state.differenceMode = elements.diffMode.value;
        state.pastLyDiff = elements.pastLyDiff.checked;
        state.futureSpitDiff = elements.futureSpitDiff.checked;
        state.futureLyDiff = elements.futureLyDiff.checked;

        const dataset = Data.generateDataset({
            startDate: state.startDate,
            endDate: state.endDate,
            lyComparisonType: state.lyComparisonType,
            today: Data.formatDateKey(todayUtc)
        });
        elements.validation.hidden = dataset.validation.valid;
        elements.validation.textContent = dataset.validation.error || "";
        if (!dataset.validation.valid) {
            elements.tableMount.innerHTML = "";
            elements.rangeSummary.textContent = "Adjust the preview range";
            return;
        }
        elements.rangeSummary.textContent = `${dataset.validation.dayCount}-day simulated view · ${state.lyComparisonType === "sameWeekday" ? "same weekday LY" : "same date LY"}`;
        const rows = Data.buildRows(dataset, {
            mode: state.mode,
            hotelCode: state.hotelCode,
            enabledCategories: [...state.enabledCategories],
            enabledHotels: [...state.enabledHotels]
        });
        renderTable(dataset, rows);
    }

    function updateViewMode(mode) {
        state.mode = mode;
        document.querySelectorAll("[data-view-mode]").forEach((button) => {
            button.setAttribute("aria-pressed", String(button.dataset.viewMode === mode));
        });
        const isComparison = mode === "comparison";
        elements.singleHotelControl.hidden = isComparison;
        elements.categoryControl.hidden = isComparison;
        elements.hotelVisibilityControl.hidden = !isComparison;
        render();
    }

    function readSelected(container) {
        return new Set(Array.from(container.querySelectorAll('input[type="checkbox"]:checked'), ({ value }) => value));
    }

    function curveSvg(points) {
        const width = 640;
        const height = 250;
        const margin = { top: 18, right: 18, bottom: 42, left: 44 };
        const plotWidth = width - margin.left - margin.right;
        const plotHeight = height - margin.top - margin.bottom;
        const maxValue = Math.max(1, ...points.flatMap((point) => [point.today, point.comparison]));
        const x = (index) => margin.left + index / (points.length - 1) * plotWidth;
        const y = (value) => margin.top + plotHeight - value / maxValue * plotHeight;
        const line = (key) => points.map((point, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(point[key]).toFixed(1)}`).join(" ");
        const grid = [0, 0.5, 1].map((ratio) => `<line x1="${margin.left}" x2="${width - margin.right}" y1="${y(maxValue * ratio)}" y2="${y(maxValue * ratio)}"></line><text x="${margin.left - 8}" y="${y(maxValue * ratio) + 4}">${Math.round(maxValue * ratio)}</text>`).join("");
        const labels = [0, 6, 12].map((index) => `<text class="pickup-x-label" x="${x(index)}" y="${height - 14}">${points[index].daysBeforeStay}d</text>`).join("");
        return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Simulated current and comparison pickup curves"><g class="pickup-grid">${grid}${labels}</g><path class="pickup-comparison-line" d="${line("comparison")}"></path><path class="pickup-current-line" d="${line("today")}"></path></svg>`;
    }

    function openDetail(button) {
        const metric = button.dataset.detailMetric;
        const detail = Data.getDetailData({
            hotelCode: button.dataset.detailHotel,
            categoryCode: button.dataset.detailCategory || null,
            date: button.dataset.detailDate,
            metric
        });
        elements.detailTitle.textContent = `${metricLabels[metric]} detail`;
        elements.detailContext.textContent = `${detail.hotel.name} · ${detail.category?.name || "All categories"} · ${formatDateLabel(button.dataset.detailDate)}`;
        elements.detailBreakdown.innerHTML = detail.breakdown.map((row) => `<tr><td>${escapeHtml(row.name)}</td><td>${row.rooms}</td><td>${row.share.toFixed(1)}%</td><td>${new Intl.NumberFormat("en-SE").format(row.averagePrice)} kr</td></tr>`).join("");
        elements.pickupCurve.innerHTML = curveSvg(detail.curve);
        elements.dialog.showModal();
    }

    document.querySelectorAll("[data-view-mode]").forEach((button) => button.addEventListener("click", () => updateViewMode(button.dataset.viewMode)));
    elements.hotel.addEventListener("change", render);
    elements.startDate.addEventListener("input", render);
    elements.endDate.addEventListener("input", render);
    elements.lyBasis.addEventListener("change", render);
    elements.diffMode.addEventListener("change", render);
    elements.pastLyDiff.addEventListener("change", render);
    elements.futureSpitDiff.addEventListener("change", render);
    elements.futureLyDiff.addEventListener("change", render);
    elements.categoryOptions.addEventListener("change", () => { state.enabledCategories = readSelected(elements.categoryOptions); render(); });
    elements.hotelVisibilityOptions.addEventListener("change", () => { state.enabledHotels = readSelected(elements.hotelVisibilityOptions); render(); });
    elements.highlights.addEventListener("change", () => { state.highlightedMetrics = readSelected(elements.highlights); render(); });
    elements.tableMount.addEventListener("click", (event) => {
        const button = event.target.closest("button[data-detail-metric]");
        if (button) openDetail(button);
    });
    elements.closeDialog.addEventListener("click", () => elements.dialog.close());
    elements.dialog.addEventListener("click", (event) => {
        if (event.target === elements.dialog) elements.dialog.close();
    });

    initializeControls();
    render();
}());
