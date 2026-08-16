(function initializeCostDataView() {
    "use strict";

    const API_URL = "/api/costdata/facts";
    // "period" columns are the bucket's first date, so they must be labelled
    // with the selected grain rather than as a plain date; "decimal" columns
    // are SEK and are rendered by the shared whole-krona formatter.
    const DATASETS = {
        roomRevenue: {
            title: "Room revenue",
            columns: [
                ["stayDate", "Period", "period"],
                ["hotelName", "Hotel", "text"],
                ["amountCurrency", "Currency", "text"],
                ["roomRevenueExclProducts1Net", "Room excl. products", "decimal"],
                ["productRevenue1Net", "Product revenue", "decimal"],
                ["roomRevenueInclProducts1Net", "Room incl. products", "decimal"],
                ["lastUpdatedAt", "Updated", "datetime"]
            ]
        },
        payments: {
            title: "Payments",
            columns: [
                ["stayDate", "Period", "period"],
                ["hotelName", "Hotel", "text"],
                ["amountCurrency", "Currency", "text"],
                ["totalPaymentAmountGrossValue", "Total payment gross", "decimal"],
                ["lastUpdatedAt", "Updated", "datetime"]
            ]
        },
        breakfast: {
            title: "Breakfast",
            columns: [
                ["stayDate", "Period", "period"],
                ["hotelName", "Hotel", "text"],
                ["breakfastTotal", "Breakfasts", "integer"],
                ["breakfastNetCost", "Net cost", "decimal"],
                ["lastUpdatedAt", "Updated", "datetime"]
            ]
        },
        parking: {
            title: "Parking",
            columns: [
                ["stayDate", "Period", "period"],
                ["hotelName", "Hotel", "text"],
                ["service", "Service", "text"],
                ["totalReservationsUsingParking", "Reservations", "integer"],
                ["totalParkingSpots", "Parking spots", "integer"],
                ["totalParkingAmountNetValue", "Net value", "decimal"],
                ["lastUpdatedAt", "Updated", "datetime"]
            ]
        },
        arrivalsDepartures: {
            title: "Arrivals & departures",
            columns: [
                ["stayDate", "Period", "period"],
                ["hotelName", "Hotel", "text"],
                ["totalArrivals", "Arrivals", "integer"],
                ["totalDepartures", "Departures", "integer"],
                ["lastUpdatedAt", "Updated", "datetime"]
            ]
        }
    };

    const elements = {
        startDate: document.getElementById("costStartDate"),
        endDate: document.getElementById("costEndDate"),
        hotel: document.getElementById("costHotel"),
        grain: document.getElementById("costGrain"),
        loadButton: document.getElementById("costLoadButton"),
        status: document.getElementById("costStatus"),
        scope: document.getElementById("costScope"),
        gop: document.getElementById("gopStatement"),
        gopRows: document.getElementById("gopRows"),
        gopFlags: document.getElementById("gopFlags"),
        gopScope: document.getElementById("gopScopeNote"),
        gopChart: document.getElementById("gopChartPanel"),
        gopChartCanvas: document.getElementById("gopChart"),
        lineToggles: document.getElementById("gopLineToggles"),
        lineReset: document.getElementById("gopLineResetButton"),
        results: document.getElementById("costResults"),
        error: document.getElementById("costError"),
        title: document.getElementById("costTableTitle"),
        rowCount: document.getElementById("costRowCount"),
        head: document.getElementById("costTableHead"),
        body: document.getElementById("costTableBody"),
        freshness: document.getElementById("freshness"),
        latestUpdate: document.getElementById("latestUpdate")
    };

    // Colours are declared here rather than only in CSS because the SVG legend
    // swatches and the tooltip dots are built in JS and must not drift from the
    // marks they describe.
    const CHART_COLOURS = Object.freeze({
        base: "#475467",
        profit: "#067647",
        loss: "#b42318",
        revenue: "#7c3aed"
    });

    const integerFormatter = new Intl.NumberFormat("en-SE", { maximumFractionDigits: 0 });
    const dateTimeFormatter = new Intl.DateTimeFormat("en-SE", {
        year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
    });

    let loadedData = null;
    let loadedSettings = {};
    let activeDataset = "roomRevenue";
    // Every group counts until it is switched off in Query settings. Clearing
    // one takes it out of the statement, out of GOP and out of the chart, so
    // the two views never disagree about what is being counted.
    const activeLines = new Set(CostData.TOGGLEABLE_KEYS);

    function localIsoDate(date) {
        const offset = date.getTimezoneOffset() * 60000;
        return new Date(date.getTime() - offset).toISOString().slice(0, 10);
    }

    function setDefaultDates() {
        const today = new Date();
        elements.startDate.value = `${today.getFullYear()}-01-01`;
        elements.endDate.value = localIsoDate(today);
    }

    function validateDates() {
        if (!elements.startDate.value || !elements.endDate.value) {
            throw new Error("Start date and end date are required.");
        }
        if (elements.startDate.value > elements.endDate.value) {
            throw new Error("Start date cannot be after end date.");
        }
    }

    function setLoading(value) {
        elements.loadButton.disabled = value;
        elements.loadButton.textContent = value ? "Updating…" : "Update data";
        elements.startDate.disabled = value;
        elements.endDate.disabled = value;
        document.querySelector(".cost-workspace").setAttribute("aria-busy", String(value));
    }

    async function loadData() {
        elements.error.hidden = true;
        try {
            validateDates();
            setLoading(true);
            elements.status.textContent = "Loading cost data…";
            const parameters = new URLSearchParams({
                startDate: elements.startDate.value,
                endDate: elements.endDate.value
            });
            const payload = await LosApi.fetchJson(`${API_URL}?${parameters}`);
            loadedData = payload.data || {};
            // The cost rulebook travels with the facts, so every figure below is
            // computed from what is currently saved in Cost Input.
            loadedSettings = payload.costSettings || {};
            populateHotels(payload.hotels || []);
            updateFreshness();
            render();
            const totalRows = Object.values(payload.rowCounts || {})
                .reduce((total, count) => total + Number(count || 0), 0);
            elements.status.textContent = totalRows
                ? "Cost data is up to date."
                : "No cost data was found for this period.";
        }
        catch (error) {
            console.error(error);
            elements.error.textContent = error.message || "Unable to update cost data.";
            elements.error.hidden = false;
            elements.status.textContent = "Update failed.";
            // The previous query's figures must not stay on screen under the new
            // header controls: the error panel sits well below the fold, so the
            // page would otherwise read as a successful answer to the new query.
            elements.gop.hidden = true;
            elements.gopChart.hidden = true;
            elements.results.hidden = true;
            elements.scope.textContent = "";
        }
        finally {
            setLoading(false);
        }
    }

    function populateHotels(hotels) {
        const selected = elements.hotel.value;
        elements.hotel.replaceChildren(new Option("All hotels", ""));
        for (const hotel of hotels) elements.hotel.add(new Option(hotel, hotel));
        if (hotels.includes(selected)) elements.hotel.value = selected;
        elements.hotel.disabled = hotels.length === 0;
    }

    function allRows() {
        return Object.values(loadedData || {}).flat();
    }

    function updateFreshness() {
        const timestamps = allRows().map((row) => row.lastUpdatedAt).filter(Boolean).sort();
        if (!timestamps.length) {
            elements.freshness.hidden = true;
            return;
        }
        elements.latestUpdate.textContent = formatDateTime(timestamps[timestamps.length - 1]);
        elements.freshness.hidden = false;
    }

    function render() {
        if (!loadedData) return;
        renderGop();
        renderTable();
        elements.scope.textContent = [
            elements.hotel.value || "All hotels",
            `${elements.startDate.value} – ${elements.endDate.value}`
        ].join(" · ");
        elements.gop.hidden = false;
        elements.gopChart.hidden = false;
        elements.results.hidden = false;
    }

    // ---------------------------------------------------------------------
    // Which groups are counted
    //
    // One checkbox per statement line, built from the statement's own line
    // list so a line added to the calculation cannot be missing here. All of
    // them start on: the default reading of the page is the whole statement.
    // ---------------------------------------------------------------------
    function buildLineToggles() {
        elements.lineToggles.replaceChildren(...CostData.GOP_LINES
            .filter((line) => line.key !== "gop")
            .map((line) => {
                const row = document.createElement("label");
                row.className = `line-toggle is-${line.type}`;
                const box = document.createElement("input");
                box.type = "checkbox";
                box.checked = activeLines.has(line.key);
                box.value = line.key;
                box.addEventListener("change", () => {
                    if (box.checked) activeLines.add(line.key);
                    else activeLines.delete(line.key);
                    syncLineToggles();
                    render();
                });
                const name = document.createElement("span");
                name.textContent = line.label;
                row.append(box, name);
                return row;
            }));
        syncLineToggles();
    }

    function syncLineToggles() {
        elements.lineReset.hidden = activeLines.size === CostData.TOGGLEABLE_KEYS.length;
    }

    function showEveryLine() {
        for (const key of CostData.TOGGLEABLE_KEYS) activeLines.add(key);
        for (const box of elements.lineToggles.querySelectorAll("input")) box.checked = true;
        syncLineToggles();
        render();
    }

    // A cost is money out; showing it as a bare positive number next to revenue
    // makes the statement impossible to read down the column. A correction
    // period can produce a negative cost, which is a credit and reads as one.
    //
    // Zero gets no sign at all. "−0" is not a smaller number than 0, it is a
    // rounding artefact, and a column of them reads as a column of tiny debits.
    function signedCost(amount) {
        const rounded = LosFormat.roundSek(amount) || 0;
        if (rounded === 0) return LosFormat.formatSek(0);
        return rounded < 0
            ? `+${LosFormat.formatSek(-rounded)}`
            : `−${LosFormat.formatSek(rounded)}`;
    }

    // The GOP statement is net of VAT throughout: every figure is a net revenue
    // stream or a cost derived from Cost Input. No gross figure appears here.
    function renderGop() {
        const statement = CostData.calculateGop(loadedData, {
            hotelName: elements.hotel.value,
            settingsByHotel: loadedSettings,
            grain: elements.grain.value,
            activeLines: Array.from(activeLines)
        });

        elements.gopScope.textContent = statement.hotels.length
            ? `${statement.currency} · net excl. VAT · ${statement.hotels.length} `
                + `${statement.hotels.length === 1 ? "hotel" : "hotels"}`
            : "No hotels selected";

        elements.gopRows.replaceChildren(...statement.lines.map((line) => {
            const row = document.createElement("tr");
            row.className = `gop-row is-${line.type}`;
            const label = document.createElement("th");
            label.scope = "row";
            label.textContent = line.label;
            const amount = document.createElement("td");
            amount.className = "gop-amount";
            amount.textContent = line.type === "cost"
                ? signedCost(line.amount)
                : LosFormat.formatSek(line.amount);
            if (line.type === "result" && line.amount < 0) amount.classList.add("is-negative");
            row.append(label, amount);
            return row;
        }));

        elements.gopFlags.replaceChildren(...statement.flags.map((message) => {
            const item = document.createElement("li");
            item.textContent = message;
            return item;
        }));
        elements.gopFlags.hidden = statement.flags.length === 0;

        renderGopChart(statement);
    }

    // ---------------------------------------------------------------------
    // The period chart
    //
    // One bar per period. The dark grey bar is the base amount - what the
    // period cost to run. Above it, green is profit; where revenue falls short
    // the red band covers the uncovered part of the cost, so the grey visibly
    // stops at the revenue line. The purple marker sits at revenue in both
    // cases, which is what makes a row of bars scannable: anything with grey
    // showing above the marker lost money.
    // ---------------------------------------------------------------------
    const SVG_NS = "http://www.w3.org/2000/svg";

    function svgNode(name, attributes) {
        const node = document.createElementNS(SVG_NS, name);
        for (const [key, value] of Object.entries(attributes || {})) {
            node.setAttribute(key, String(value));
        }
        return node;
    }

    function chartEmpty(message) {
        elements.gopChartCanvas.replaceChildren();
        const note = document.createElement("p");
        note.className = "chart-empty";
        note.textContent = message;
        elements.gopChartCanvas.append(note);
    }

    // Zero rounds up to zero, not to one. Returning a headroom of 1 kr for an
    // empty side put the baseline of an ordinary all-positive chart at -1 and
    // drew a zero line one pixel above the axis on every period.
    function niceCeiling(value) {
        if (!(value > 0)) return 0;
        const magnitude = 10 ** Math.floor(Math.log10(value));
        for (const step of [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10]) {
            if (value <= step * magnitude) return step * magnitude;
        }
        return 10 * magnitude;
    }

    const compactFormatter = new Intl.NumberFormat("en-SE", {
        notation: "compact", maximumFractionDigits: 1
    });

    function renderGopChart(statement) {
        const periods = statement.periods || [];
        if (!periods.length) {
            chartEmpty("Nothing to chart for the hotels and period you selected.");
            return;
        }

        const grain = elements.grain.value;
        const width = 1100;
        const height = 380;
        const margin = { top: 20, right: 24, bottom: 74, left: 76 };
        const plotWidth = width - margin.left - margin.right;
        const plotHeight = height - margin.top - margin.bottom;
        // A correction period can reverse more than it books, so revenue and
        // cost are both allowed to be negative. The axis therefore has to span
        // both sides of zero: clamping the scale at zero drew a half-million
        // krona reversal as a few pixels of red under a tooltip reporting the
        // real figure.
        const values = periods.flatMap((period) => [period.revenue, period.cost]);
        const maxValue = niceCeiling(Math.max(0, ...values));
        const minValue = -niceCeiling(-Math.min(0, ...values));
        const span = maxValue - minValue || 1;
        const y = (value) =>
            margin.top + plotHeight - ((value - minValue) / span) * plotHeight;
        const zeroLine = y(0);
        // Every band is "from one value to another", which keeps the geometry
        // identical whichever side of zero the two ends fall on.
        const verticalBand = (from, to) => ({
            y: Math.min(y(from), y(to)),
            height: Math.abs(y(from) - y(to))
        });
        // Bars keep breathing room at both ends of the band so a single period
        // is not stretched across the whole plot.
        const band = plotWidth / periods.length;
        const barWidth = Math.max(4, Math.min(64, band * 0.62));
        const centre = (index) => margin.left + band * (index + 0.5);

        const svg = svgNode("svg", {
            viewBox: `0 0 ${width} ${height}`,
            role: "img",
            "aria-label":
                "Base cost, profit or loss, and revenue level for each period"
        });

        for (let tick = 0; tick <= 4; tick += 1) {
            const value = minValue + span * tick / 4;
            svg.append(svgNode("line", {
                x1: margin.left, x2: width - margin.right,
                y1: y(value), y2: y(value), class: "chart-grid-line"
            }));
            const label = svgNode("text", {
                x: margin.left - 12, y: y(value) + 4,
                class: "chart-axis-label chart-y-label"
            });
            label.textContent = compactFormatter.format(value);
            svg.append(label);
        }

        // Bars grow from zero in both directions, so the zero line has to be
        // readable as a baseline rather than as one grid line among five.
        if (minValue < 0) {
            svg.append(svgNode("line", {
                x1: margin.left, x2: width - margin.right,
                y1: zeroLine, y2: zeroLine, class: "gop-bar-zero"
            }));
        }

        const labelStep = Math.max(1, Math.ceil(periods.length / 14));
        periods.forEach((period, index) => {
            const revenue = period.revenue;
            const cost = period.cost;
            const left = centre(index) - barWidth / 2;

            // The base runs from the zero line to the cost, downwards when a
            // correction makes the period's cost negative.
            const base = verticalBand(0, cost);
            svg.append(svgNode("rect", {
                x: left, y: base.y, width: barWidth, height: base.height,
                class: "gop-bar-base", fill: CHART_COLOURS.base
            }));

            // Profit and loss occupy the same span - cost to revenue - and
            // differ only in which way round they sit, so only the colour
            // changes. The loss band is drawn over the base, which is what
            // makes the grey visibly stop at the revenue level.
            if (revenue !== cost) {
                const result = verticalBand(cost, revenue);
                svg.append(svgNode("rect", {
                    x: left, y: result.y, width: barWidth, height: result.height,
                    class: revenue > cost ? "gop-bar-profit" : "gop-bar-loss",
                    fill: revenue > cost ? CHART_COLOURS.profit : CHART_COLOURS.loss
                }));
            }

            // The marker spans the bar and nothing more. It used to overhang by
            // 5px each side, which at a daily grain - where the bars are only a
            // few pixels apart - drew it straight across its neighbours.
            const marker = barWidth / 2;
            svg.append(svgNode("line", {
                x1: centre(index) - marker, x2: centre(index) + marker,
                y1: y(revenue), y2: y(revenue),
                class: "gop-bar-revenue", stroke: CHART_COLOURS.revenue
            }));

            if (index % labelStep !== 0 && index !== periods.length - 1) return;
            const parts = LosFormat.periodLabelParts(period.periodKey, grain);
            const tick = svgNode("text", {
                x: centre(index), y: height - 38,
                class: "chart-axis-label chart-x-label chart-period-label"
            });
            tick.textContent = parts.primary;
            svg.append(tick);
            if (parts.year) {
                const year = svgNode("text", {
                    x: centre(index), y: height - 20,
                    class: "chart-axis-label chart-x-label chart-year-label"
                });
                year.textContent = parts.year;
                svg.append(year);
            }
        });

        const tooltip = document.createElement("div");
        tooltip.className = "chart-tooltip";
        tooltip.hidden = true;

        function showPeriod(index) {
            const period = periods[index];
            tooltip.hidden = false;
            tooltip.classList.toggle("align-right", centre(index) > width * 0.72);
            tooltip.style.left = `${centre(index) / width * 100}%`;
            tooltip.replaceChildren(
                tooltipTitle(LosFormat.periodLabel(period.periodKey, grain)),
                tooltipRow("Revenue", CHART_COLOURS.revenue, period.revenue),
                tooltipRow("Base cost", CHART_COLOURS.base, period.cost),
                tooltipRow(
                    period.gop < 0 ? "Loss" : "Profit",
                    period.gop < 0 ? CHART_COLOURS.loss : CHART_COLOURS.profit,
                    period.gop
                )
            );
        }

        periods.forEach((period, index) => {
            const hit = svgNode("rect", {
                x: margin.left + band * index, y: margin.top,
                width: band, height: plotHeight,
                class: "chart-hit-area", tabindex: "0",
                "aria-label":
                    `${LosFormat.periodLabel(period.periodKey, grain)}: `
                    + `revenue ${LosFormat.formatSek(period.revenue)}, `
                    + `base cost ${LosFormat.formatSek(period.cost)}, `
                    + `${period.gop < 0 ? "loss" : "profit"} `
                    + `${LosFormat.formatSek(Math.abs(period.gop))}`
            });
            hit.addEventListener("mouseenter", () => showPeriod(index));
            hit.addEventListener("focus", () => showPeriod(index));
            hit.addEventListener("mouseleave", () => { tooltip.hidden = true; });
            hit.addEventListener("blur", () => { tooltip.hidden = true; });
            svg.append(hit);
        });

        elements.gopChartCanvas.replaceChildren(svg, tooltip);
    }

    function tooltipTitle(text) {
        const title = document.createElement("strong");
        title.className = "chart-tooltip-title";
        title.textContent = text;
        return title;
    }

    function tooltipRow(label, colour, amount) {
        const row = document.createElement("div");
        row.className = "chart-tooltip-series";
        const name = document.createElement("span");
        const swatch = document.createElement("i");
        swatch.style.background = colour;
        name.append(swatch, document.createTextNode(label));
        const value = document.createElement("strong");
        value.textContent = LosFormat.formatSekAmount(amount);
        row.append(name, value);
        return row;
    }

    function renderTable() {
        const definition = DATASETS[activeDataset];
        const grain = elements.grain.value;
        const rows = CostData.aggregate(activeDataset, loadedData[activeDataset] || [], {
            grain,
            hotelName: elements.hotel.value
        });
        elements.title.textContent = definition.title;
        elements.rowCount.textContent = `${integerFormatter.format(rows.length)} ${rows.length === 1 ? "row" : "rows"}`;
        elements.head.replaceChildren(buildHeader(definition.columns));
        elements.body.replaceChildren(...buildRows(rows, definition.columns, grain));
    }

    function buildHeader(columns) {
        const row = document.createElement("tr");
        for (const [, label, type] of columns) {
            const header = document.createElement("th");
            header.scope = "col";
            header.textContent = label;
            if (["integer", "decimal"].includes(type)) header.className = "numeric-column";
            row.appendChild(header);
        }
        return row;
    }

    function buildRows(rows, columns, grain) {
        if (!rows.length) {
            const row = document.createElement("tr");
            const cell = document.createElement("td");
            cell.colSpan = columns.length;
            cell.className = "cost-empty";
            cell.textContent = "No rows for the hotels and period you selected.";
            row.appendChild(cell);
            return [row];
        }

        return rows.map((item) => {
            const row = document.createElement("tr");
            for (const [field, , type] of columns) {
                const cell = document.createElement("td");
                cell.textContent = formatCell(item[field], type, grain);
                if (["integer", "decimal"].includes(type)) cell.className = "numeric-column";
                row.appendChild(cell);
            }
            return row;
        });
    }

    function formatCell(value, type, grain) {
        if (value === null || value === undefined || value === "") return "—";
        // Adding 0 collapses -0 to 0. Intl formats -0 as "-0", which is a
        // rounding artefact rather than a quantity and reads as an error.
        if (type === "integer") return integerFormatter.format(Number(value) + 0);
        if (type === "decimal") return LosFormat.formatSek(value);
        if (type === "datetime") return formatDateTime(value);
        if (type === "period") return LosFormat.periodLabel(value, grain);
        return String(value);
    }

    function formatDateTime(value) {
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? "—" : dateTimeFormatter.format(date);
    }

    function selectDataset(button) {
        activeDataset = button.dataset.dataset;
        for (const tab of document.querySelectorAll("[data-dataset]")) {
            const selected = tab === button;
            tab.setAttribute("aria-selected", String(selected));
            tab.tabIndex = selected ? 0 : -1;
        }
        document.getElementById("costTablePanel").setAttribute("aria-labelledby", button.id);
        renderTable();
    }

    elements.loadButton.addEventListener("click", loadData);
    elements.hotel.addEventListener("change", render);
    // The grain decides the chart's buckets as well as the table's, so it can
    // no longer redraw the table alone.
    elements.grain.addEventListener("change", render);
    elements.lineReset.addEventListener("click", showEveryLine);
    for (const tab of document.querySelectorAll("[data-dataset]")) {
        tab.addEventListener("click", () => selectDataset(tab));
        tab.addEventListener("keydown", (event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            const tabs = Array.from(document.querySelectorAll("[data-dataset]"));
            let next;
            if (event.key === "Home") next = tabs[0];
            else if (event.key === "End") next = tabs[tabs.length - 1];
            else {
                const direction = event.key === "ArrowRight" ? 1 : -1;
                next = tabs[(tabs.indexOf(tab) + direction + tabs.length) % tabs.length];
            }
            event.preventDefault();
            next.focus();
            selectDataset(next);
        });
    }

    setDefaultDates();
    buildLineToggles();
}());
