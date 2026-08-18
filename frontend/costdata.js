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
        chartGrain: document.getElementById("gopChartGrain"),
        showLyFinal: document.getElementById("gopChartShowLyFinal"),
        showSpit: document.getElementById("gopChartShowSpit"),
        lyBasis: document.getElementById("gopChartLyBasis"),
        chartNote: document.getElementById("gopChartNote"),
        comparisonNote: document.getElementById("gopComparisonNote"),
        lyLegend: document.querySelectorAll(".gop-legend-ly-entry"),
        comparisonCostLabel: document.getElementById("gopComparisonCostLabel"),
        comparisonRevenueLabel: document.getElementById("gopComparisonRevenueLabel"),
        chartReset: document.getElementById("gopChartReset"),
        chartTimeline: document.getElementById("gopChartTimeline"),
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

    // Last year, in the same four roles at a fraction of the weight. Same hues
    // rather than four new ones: the reader has to see "the cost bar, last year",
    // not a second colour scheme to learn - and the pairs stay distinguishable
    // when they sit side by side a few pixels apart.
    const LY_COLOURS = Object.freeze({
        base: "#a9b2c0",
        profit: "#83c8a5",
        loss: "#e8a29c",
        revenue: "#c0aaf3"
    });

    const integerFormatter = new Intl.NumberFormat("en-SE", { maximumFractionDigits: 0 });
    const dateTimeFormatter = new Intl.DateTimeFormat("en-SE", {
        year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
    });

    let loadedData = null;
    let loadedSettings = {};
    // The range the loaded facts actually cover, which is not the same thing as
    // what the date inputs currently say: the comparison has to be fetched for
    // the range on screen, not for one the reader has typed but not applied yet.
    let loadedRange = null;
    // Last year's facts, as {key, data}. Fetched only when the comparison is
    // switched on, and re-fetched when the range or the basis changes.
    let comparison = null;
    let comparisonMode = null;
    let chartZoom = null;
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
        // The comparison controls can each start a request of their own, so they
        // are held while one is already in flight.
        if (elements.showLyFinal) elements.showLyFinal.disabled = value;
        if (elements.showSpit) elements.showSpit.disabled = value;
        if (elements.lyBasis) {
            elements.lyBasis.disabled = value || !comparisonMode;
        }
        document.querySelector(".cost-workspace").setAttribute("aria-busy", String(value));
    }

    // ---------------------------------------------------------------------
    // Last year
    //
    // The two bases and the date arithmetic behind them live in los-format.js,
    // beside the period keys they have to stay consistent with, and are unit
    // tested there: 29 February and the 364-day offset are both easy to get
    // subtly wrong in a way no total on this page would reveal.
    // ---------------------------------------------------------------------

    function comparisonKey() {
        if (!loadedRange) return "";
        return `${loadedRange.startDate}|${loadedRange.endDate}|${elements.lyBasis.value}|${comparisonMode}`;
    }

    function syncComparisonControls() {
        // A basis that changes nothing while the comparison is off is a control
        // that lies about having an effect.
        if (elements.lyBasis) elements.lyBasis.disabled = !comparisonMode;
        if (elements.showLyFinal) {
            elements.showLyFinal.setAttribute(
                "aria-pressed", String(comparisonMode === "final")
            );
        }
        if (elements.showSpit) {
            elements.showSpit.setAttribute(
                "aria-pressed", String(comparisonMode === "spit")
            );
        }
    }

    // Driven by what was actually drawn, not by the checkbox: a comparison that
    // failed to load leaves the box ticked, and a legend naming marks that are
    // not on the chart is worse than no legend entry at all.
    function syncComparisonLegend(drawn) {
        for (const entry of elements.lyLegend) entry.hidden = !drawn;
        const label = comparisonMode === "spit" ? "SPIT" : "LY Final";
        if (elements.comparisonCostLabel) {
            elements.comparisonCostLabel.textContent = `${label} cost`;
        }
        if (elements.comparisonRevenueLabel) {
            elements.comparisonRevenueLabel.textContent = `${label} revenue`;
        }
    }

    function comparisonName() {
        return comparisonMode === "spit" ? "SPIT" : "LY Final";
    }

    /**
     * Fetch the comparison for an explicit range.
     *
     * Takes the range rather than reading loadedRange, so a load can start this
     * before it has one - the comparison is a fixed offset from what the date
     * inputs already say, and never needed this year's response to begin.
     */
    async function loadComparison(forRange, key) {
        const range = LosFormat.lastYearRange(forRange, elements.lyBasis.value);
        elements.comparisonNote.hidden = true;
        try {
            const parameters = new URLSearchParams({
                ...forRange,
                includeComparison: "true",
                lyComparisonBasis: elements.lyBasis.value,
                comparisonMode
            });
            const payload = await LosApi.fetchJson(
                `${API_URL}?${parameters}`
            );
            const candidate = payload.comparison || {};
            if (comparisonMode === "spit" && !candidate.spit?.available) {
                throw new Error("the historical lifecycle snapshot is not available");
            }
            comparison = {
                key,
                mode: comparisonMode,
                data: candidate.data || {},
                spit: candidate.spit || null
            };
        }
        catch (error) {
            console.error(error);
            comparison = null;
            // Deliberately not the page's error panel, and deliberately not
            // clearing the statement: this year's figures are still complete and
            // correct, and hiding them over a comparison that is an extra reading
            // would be the larger loss.
            const label = comparisonMode === "spit" ? "SPIT" : "LY Final";
            elements.comparisonNote.textContent =
                `${label} (${range.startDate} – ${range.endDate}) could not be loaded: `
                + `${error.message || "the request failed"}. The chart shows this year only.`;
            elements.comparisonNote.hidden = false;
        }
    }

    // Busy state belongs to the caller: loadData already holds the controls
    // through setLoading, and doing it here as well released them halfway
    // through a load that was still running.
    async function ensureComparison() {
        if (!comparisonMode || !loadedRange) {
            comparison = null;
            elements.comparisonNote.hidden = true;
            return;
        }
        const key = comparisonKey();
        if (comparison && comparison.key === key) return;
        await loadComparison(loadedRange, key);
    }

    async function loadData() {
        elements.error.hidden = true;
        try {
            validateDates();
            setLoading(true);
            elements.status.textContent = "Loading cost data…";
            const range = {
                startDate: elements.startDate.value,
                endDate: elements.endDate.value
            };
            const parameters = new URLSearchParams(range);
            const wantsComparison = Boolean(comparisonMode);
            if (wantsComparison) {
                parameters.set("includeComparison", "true");
                parameters.set("lyComparisonBasis", elements.lyBasis.value);
                parameters.set("comparisonMode", comparisonMode);
            }

            let payload;
            let comparisonFailure = null;
            try {
                // One Function invocation now owns the selected and comparison
                // ranges. The server runs them concurrently under one global
                // connection ceiling, returns the rulebook once and caches the
                // complete compressed body against its Database A publication.
                payload = await LosApi.fetchJson(`${API_URL}?${parameters}`);
            }
            catch (error) {
                if (!wantsComparison) throw error;
                // Preserve the established partial-failure contract: a problem
                // building the optional comparison must not hide a valid current
                // statement. Retry only the smaller current-only response.
                console.error(error);
                comparisonFailure = error;
                payload = await LosApi.fetchJson(
                    `${API_URL}?${new URLSearchParams(range)}`
                );
            }
            loadedData = payload.data || {};
            // The cost rulebook travels with the facts, so every figure below is
            // computed from what is currently saved in Cost Input.
            loadedSettings = payload.costSettings || {};
            loadedRange = range;
            chartZoom = null;
            if (elements.chartGrain) {
                elements.chartGrain.disabled = false;
                elements.chartGrain.value = elements.grain.value;
            }
            populateHotels(payload.hotels || []);
            updateFreshness();

            if (wantsComparison && comparisonMode === "spit"
                && payload.comparison && !payload.comparison.spit?.available) {
                comparisonFailure = new Error(
                    "the historical lifecycle snapshot is not available"
                );
            }
            if (wantsComparison && payload.comparison && !comparisonFailure) {
                comparison = {
                    key: `${range.startDate}|${range.endDate}|${elements.lyBasis.value}|${comparisonMode}`,
                    mode: comparisonMode,
                    data: payload.comparison.data || {},
                    spit: payload.comparison.spit || null
                };
                elements.comparisonNote.hidden = true;
            }
            else if (wantsComparison) {
                const previous = LosFormat.lastYearRange(
                    range,
                    elements.lyBasis.value
                );
                comparison = null;
                const label = comparisonMode === "spit" ? "SPIT" : "LY Final";
                elements.comparisonNote.textContent =
                    `${label} (${previous.startDate} â€“ ${previous.endDate}) could not be loaded: `
                    + `${comparisonFailure?.message || "the request failed"}. The chart shows this year only.`;
                elements.comparisonNote.hidden = false;
            }
            else {
                comparison = null;
                elements.comparisonNote.hidden = true;
            }

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

    function dataInRange(source, startDate, endDate) {
        const result = {};
        for (const [dataset, rows] of Object.entries(source || {})) {
            result[dataset] = (rows || []).filter(
                (row) => !row?.stayDate
                    || (row.stayDate >= startDate && row.stayDate <= endDate)
            );
        }
        return result;
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

        // Costed under today's rulebook, not last year's: Cost Input is not
        // versioned, so the comparison answers "what would last year's volumes
        // cost to run now" rather than "what did it cost then". Its flags are
        // dropped - they would repeat this year's, about the same configuration.
        let comparisonData = null;
        if (comparison) {
            const source = comparison.mode === "spit"
                ? CostData.applySpitAdjustments(
                    comparison.data,
                    comparison.spit?.adjustments || [],
                    comparison.spit?.cutoffDate || ""
                )
                : comparison.data;
            comparisonData = CostData.alignToComparison(
                source, elements.lyBasis.value, statement.hotels
            );
        }

        const chartGrain = chartZoom ? "day" : elements.grain.value;
        const chartData = chartZoom
            ? dataInRange(loadedData, chartZoom.startDate, chartZoom.endDate)
            : loadedData;
        const chartStatement = chartZoom
            ? CostData.calculateGop(chartData, {
                hotelName: elements.hotel.value,
                settingsByHotel: loadedSettings,
                grain: chartGrain,
                activeLines: Array.from(activeLines)
            })
            : statement;
        const compared = comparisonData
            ? CostData.calculateGop(
                chartZoom
                    ? dataInRange(
                        comparisonData, chartZoom.startDate, chartZoom.endDate
                    )
                    : comparisonData,
                {
                    hotelName: elements.hotel.value,
                    settingsByHotel: loadedSettings,
                    grain: chartGrain,
                    activeLines: Array.from(activeLines)
                }
            )
            : null;

        renderGopChart(chartStatement, compared, chartGrain);
        renderChartTimeline();
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

    function renderGopChart(statement, lastYear, grain) {
        const periods = statement.periods || [];
        // Matched on the period key, not on position: alignToThisYear restamped
        // last year's dates onto this year's, so the keys are the same buckets.
        const comparisonPeriods = new Map(
            ((lastYear && lastYear.periods) || []).map(
                (period) => [period.periodKey, period]
            )
        );
        const showComparison = Boolean(lastYear);
        // Before the empty check, or an emptied chart keeps the previous
        // reading's note and legend and claims two bars where there are none.
        setChartNote(showComparison);
        syncComparisonLegend(showComparison);
        if (!periods.length) {
            chartEmpty("Nothing to chart for the hotels and period you selected.");
            return;
        }

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
        // Last year's bars are drawn on this axis too, so they have to be part of
        // what sets it. Leaving them out drew a bigger last year straight through
        // the top of the plot.
        const values = periods.flatMap((period) => {
            const previous = comparisonPeriods.get(period.periodKey);
            return [
                period.revenue, period.cost,
                ...(previous ? [previous.revenue, previous.cost] : [])
            ];
        });
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
        // is not stretched across the whole plot, and with last year shown the
        // band holds a pair with a hairline between them - so two bars of the
        // same height still read as two.
        //
        // Nothing may be wider than the band it sits in. At a daily grain over a
        // year the band is a couple of pixels, and a bar with a fixed minimum
        // width draws straight across its neighbours; a pair of them would draw
        // across two.
        const band = plotWidth / periods.length;
        const groupWidth = showComparison
            ? Math.min(band, 84, Math.max(3, band * 0.78))
            : Math.min(band, 64, Math.max(3, band * 0.62));
        // The gap shrinks with the pair rather than eating it.
        const pairGap = showComparison ? Math.min(3, groupWidth * 0.2) : 0;
        const barWidth = showComparison ? (groupWidth - pairGap) / 2 : groupWidth;
        const centre = (index) => margin.left + band * (index + 0.5);
        // This year on the left, last year on the right, in every period.
        const barLeft = (index, isComparison) =>
            centre(index) - groupWidth / 2
            + (isComparison ? barWidth + pairGap : 0);

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

        // One period's stack: the base cost, the profit or loss against it, and
        // the revenue marker. Both years draw through this, so the two can never
        // drift apart in geometry - only in palette and in a modifier class.
        function drawStack(period, left, palette, isComparison) {
            const revenue = period.revenue;
            const cost = period.cost;
            const modifier = isComparison ? " is-comparison" : "";

            // The base runs from the zero line to the cost, downwards when a
            // correction makes the period's cost negative.
            const base = verticalBand(0, cost);
            svg.append(svgNode("rect", {
                x: left, y: base.y, width: barWidth, height: base.height,
                class: `gop-bar-base${modifier}`, fill: palette.base
            }));

            // Profit and loss occupy the same span - cost to revenue - and
            // differ only in which way round they sit, so only the colour
            // changes. The loss band is drawn over the base, which is what
            // makes the grey visibly stop at the revenue level.
            if (revenue !== cost) {
                const result = verticalBand(cost, revenue);
                svg.append(svgNode("rect", {
                    x: left, y: result.y, width: barWidth, height: result.height,
                    class: (revenue > cost ? "gop-bar-profit" : "gop-bar-loss") + modifier,
                    fill: revenue > cost ? palette.profit : palette.loss
                }));
            }

            // The marker spans the bar and nothing more. It used to overhang by
            // 5px each side, which at a daily grain - where the bars are only a
            // few pixels apart - drew it straight across its neighbours. With a
            // pair in the band it is narrower still, and centred on its own bar
            // rather than on the period.
            const marker = barWidth / 2;
            const barCentre = left + barWidth / 2;
            svg.append(svgNode("line", {
                x1: barCentre - marker, x2: barCentre + marker,
                y1: y(revenue), y2: y(revenue),
                class: `gop-bar-revenue${modifier}`, stroke: palette.revenue
            }));
        }

        const labelStep = Math.max(1, Math.ceil(periods.length / 14));
        periods.forEach((period, index) => {
            drawStack(period, barLeft(index, false), CHART_COLOURS, false);
            const previous = comparisonPeriods.get(period.periodKey);
            // A period with no counterpart last year draws no second bar at all.
            // Drawing a zero-height one would read as "last year earned nothing",
            // which is a different claim from "there is nothing to compare with".
            if (showComparison && previous) {
                drawStack(previous, barLeft(index, true), LY_COLOURS, true);
            }

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
            const previous = comparisonPeriods.get(period.periodKey);
            tooltip.hidden = false;
            tooltip.classList.toggle("align-right", centre(index) > width * 0.72);
            tooltip.style.left = `${centre(index) / width * 100}%`;
            const rows = [
                tooltipTitle(LosFormat.periodLabel(period.periodKey, grain)),
                tooltipRow(
                    "Revenue", CHART_COLOURS.revenue, period.revenue,
                    showComparison
                        ? varianceNote(period.revenue, previous && previous.revenue)
                        : null
                ),
                tooltipRow(
                    "Base cost", CHART_COLOURS.base, period.cost,
                    showComparison
                        ? varianceNote(period.cost, previous && previous.cost)
                        : null
                ),
                tooltipRow(
                    period.gop < 0 ? "Loss" : "Profit",
                    period.gop < 0 ? CHART_COLOURS.loss : CHART_COLOURS.profit,
                    period.gop,
                    showComparison
                        ? varianceNote(period.gop, previous && previous.gop)
                        : null
                )
            ];
            if (showComparison && previous) {
                rows.push(
                    tooltipTitle(comparisonLabel(period.periodKey)),
                    tooltipRow("Revenue", LY_COLOURS.revenue, previous.revenue),
                    tooltipRow("Base cost", LY_COLOURS.base, previous.cost),
                    tooltipRow(
                        previous.gop < 0 ? "Loss" : "Profit",
                        previous.gop < 0 ? LY_COLOURS.loss : LY_COLOURS.profit,
                        previous.gop
                    )
                );
            }
            tooltip.replaceChildren(...rows);
        }

        // The period this bar's neighbour actually is, rather than a bare "last
        // year": on a same-weekday basis it is 364 days back, which for a month
        // grain is a different month from the one the reader would assume.
        function comparisonLabel(periodKey) {
            const basis = elements.lyBasis.value === "sameWeekday"
                ? "same weekday" : "same date";
            const cutoff = comparisonMode === "spit" && comparison?.spit?.cutoffDate
                ? ` · cutoff ${comparison.spit.cutoffDate}`
                : "";
            return `${comparisonName()} · ${LosFormat.periodLabel(
                LosFormat.lastYearDate(periodKey, elements.lyBasis.value), grain
            )} · ${basis}${cutoff}`;
        }

        periods.forEach((period, index) => {
            const previous = comparisonPeriods.get(period.periodKey);
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
                    // A reader on a screen reader gets the comparison spoken, not
                    // only drawn - it is half the marks in the band.
                    + (showComparison
                        ? previous
                            ? `. ${comparisonName()}: revenue ${LosFormat.formatSek(previous.revenue)}, `
                                + `base cost ${LosFormat.formatSek(previous.cost)}, `
                                + `${previous.gop < 0 ? "loss" : "profit"} `
                                + `${LosFormat.formatSek(Math.abs(previous.gop))}`
                            : `. No matching ${comparisonName()} period`
                        : "")
            });
            hit.addEventListener("mouseenter", () => showPeriod(index));
            hit.addEventListener("focus", () => showPeriod(index));
            hit.addEventListener("mouseleave", () => { tooltip.hidden = true; });
            hit.addEventListener("blur", () => { tooltip.hidden = true; });
            svg.append(hit);
        });

        elements.gopChartCanvas.replaceChildren(svg, tooltip);
    }

    const monthFormatter = new Intl.DateTimeFormat("en-SE", {
        month: "short", year: "numeric", timeZone: "UTC"
    });

    function parseIsoDate(value) {
        const [year, month, day] = value.split("-").map(Number);
        return new Date(Date.UTC(year, month - 1, day));
    }

    function utcIsoDate(value) {
        return value.toISOString().slice(0, 10);
    }

    function daysInRange(start, end) {
        return Math.round((end.getTime() - start.getTime()) / 86400000) + 1;
    }

    function isoWeekNumber(value) {
        const thursday = new Date(value);
        const weekday = (thursday.getUTCDay() + 6) % 7;
        thursday.setUTCDate(thursday.getUTCDate() + 3 - weekday);
        const firstThursday = new Date(Date.UTC(thursday.getUTCFullYear(), 0, 4));
        const firstWeekday = (firstThursday.getUTCDay() + 6) % 7;
        firstThursday.setUTCDate(firstThursday.getUTCDate() + 3 - firstWeekday);
        return 1 + Math.round(
            (thursday.getTime() - firstThursday.getTime()) / (7 * 86400000)
        );
    }

    function timelineSegments(kind, rangeStart, rangeEnd) {
        const segments = [];
        let cursor;
        if (kind === "month") {
            cursor = new Date(Date.UTC(
                rangeStart.getUTCFullYear(), rangeStart.getUTCMonth(), 1
            ));
        }
        else {
            cursor = new Date(rangeStart);
            cursor.setUTCDate(
                cursor.getUTCDate() - ((cursor.getUTCDay() + 6) % 7)
            );
        }

        while (cursor <= rangeEnd) {
            const naturalStart = new Date(cursor);
            let naturalEnd;
            let next;
            if (kind === "month") {
                next = new Date(Date.UTC(
                    cursor.getUTCFullYear(), cursor.getUTCMonth() + 1, 1
                ));
                naturalEnd = new Date(next.getTime() - 86400000);
            }
            else {
                next = new Date(cursor.getTime() + 7 * 86400000);
                naturalEnd = new Date(cursor.getTime() + 6 * 86400000);
            }
            const start = naturalStart < rangeStart ? rangeStart : naturalStart;
            const end = naturalEnd > rangeEnd ? rangeEnd : naturalEnd;
            segments.push({
                kind,
                key: utcIsoDate(naturalStart),
                startDate: utcIsoDate(start),
                endDate: utcIsoDate(end),
                days: daysInRange(start, end),
                label: kind === "month"
                    ? monthFormatter.format(naturalStart)
                    : `W${isoWeekNumber(naturalStart)}`,
                title: kind === "month"
                    ? `Zoom to ${monthFormatter.format(naturalStart)}`
                    : `Zoom to week ${isoWeekNumber(naturalStart)}, ${naturalStart.getUTCFullYear()}`
            });
            cursor = next;
        }
        return segments;
    }

    function setChartZoom(segment) {
        chartZoom = {
            type: segment.kind,
            key: segment.key,
            startDate: segment.startDate,
            endDate: segment.endDate
        };
        if (elements.chartGrain) {
            elements.chartGrain.value = "day";
            elements.chartGrain.disabled = true;
        }
        render();
    }

    function resetChartView() {
        chartZoom = null;
        if (elements.chartGrain) {
            elements.chartGrain.disabled = false;
            elements.chartGrain.value = elements.grain.value;
        }
        render();
    }

    function renderChartTimeline() {
        if (!elements.chartTimeline || !loadedRange) return;
        const start = parseIsoDate(loadedRange.startDate);
        const end = parseIsoDate(loadedRange.endDate);
        const rows = [
            ["week", "Weeks"],
            ["month", "Months"]
        ].map(([kind, label]) => {
            const row = document.createElement("div");
            row.className = `chart-time-row is-${kind}`;
            row.setAttribute("role", "group");
            row.setAttribute("aria-label", label);
            for (const segment of timelineSegments(kind, start, end)) {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "chart-time-button";
                button.textContent = segment.label;
                button.title = segment.title;
                button.style.flexGrow = String(segment.days);
                button.setAttribute("aria-pressed", String(Boolean(
                    chartZoom
                    && chartZoom.type === segment.kind
                    && chartZoom.key === segment.key
                )));
                button.addEventListener("click", () => setChartZoom(segment));
                row.append(button);
            }
            return row;
        });
        elements.chartTimeline.replaceChildren(...rows);
        if (elements.chartReset) elements.chartReset.hidden = !chartZoom;
    }

    // Rebuilt rather than retyped as textContent, so the control's name keeps the
    // emphasis the markup gave it and the note still points at the thing the
    // reader has to change.
    function setChartNote(showComparison) {
        const control = document.createElement("strong");
        control.textContent = "Group by";
        elements.chartNote.replaceChildren(
            document.createTextNode(
                showComparison ? "Two bars per " : "One bar per "
            ),
            control,
            document.createTextNode(
                showComparison
                    ? ` period: this year on the left, ${comparisonName()} on the right. `
                        + "Bar height is what the period cost to run; the marker is "
                        + "the revenue it earned."
                    : " period. Bar height is what the period cost to run; the "
                        + "marker is the revenue it earned."
            )
        );
    }

    function tooltipTitle(text) {
        const title = document.createElement("strong");
        title.className = "chart-tooltip-title";
        title.textContent = text;
        return title;
    }

    function tooltipRow(label, colour, amount, note) {
        const row = document.createElement("div");
        row.className = "chart-tooltip-series";
        const name = document.createElement("span");
        const swatch = document.createElement("i");
        swatch.style.background = colour;
        name.append(swatch, document.createTextNode(label));
        const value = document.createElement("strong");
        value.textContent = LosFormat.formatSekAmount(amount);
        row.append(name, value);
        if (note) {
            const detail = document.createElement("small");
            detail.textContent = note;
            row.append(detail);
        }
        return row;
    }

    // How this period moved against its counterpart. Rounded before it is judged:
    // a difference of forty ore is "level with last year", and reporting it as
    // "+0 kr (+0.0%)" reads as a change nobody can find.
    function varianceNote(current, previous) {
        if (previous === null || previous === undefined) {
            return `No matching ${comparisonName()} period`;
        }
        const delta = LosFormat.roundSek(current - previous) || 0;
        if (delta === 0) return `Level with ${comparisonName()}`;
        const sign = delta > 0 ? "+" : "−";
        const size = `${sign}${LosFormat.formatSekAmount(Math.abs(delta))}`;
        // A previous figure of zero has no percentage: everything is an infinite
        // increase on nothing, which says less than the amount already does. The
        // same goes for a sign change, where a percentage of a negative base
        // points the wrong way.
        const base = LosFormat.roundSek(previous) || 0;
        if (base <= 0) return `${size} vs ${comparisonName()}`;
        const share = Math.abs(delta / base) * 100;
        return `${size} (${sign}${share.toFixed(share < 10 ? 1 : 0)}%) vs ${comparisonName()}`;
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

    // Two controls, one value. #costGrain in Query settings stays the one the
    // rest of the file reads; the copy beside the chart mirrors it in both
    // directions, so neither can be left showing a grain that is not in force.
    function setGrain(value) {
        chartZoom = null;
        if (elements.chartGrain) elements.chartGrain.disabled = false;
        if (elements.grain.value !== value) elements.grain.value = value;
        if (elements.chartGrain && elements.chartGrain.value !== value) {
            elements.chartGrain.value = value;
        }
        render();
    }

    // Switching the comparison on is the one control here that can need a
    // request, so it goes through the same busy state the Update data button does
    // rather than appearing to do nothing for a second.
    async function refreshComparison() {
        syncComparisonControls();
        if (!loadedData) return;
        setLoading(true);
        try {
            await ensureComparison();
            render();
        }
        finally { setLoading(false); }
    }

    async function toggleComparison(mode) {
        comparisonMode = comparisonMode === mode ? null : mode;
        comparison = null;
        await refreshComparison();
    }

    elements.loadButton.addEventListener("click", loadData);
    elements.hotel.addEventListener("change", render);
    if (elements.showLyFinal) {
        elements.showLyFinal.addEventListener(
            "click", () => toggleComparison("final")
        );
    }
    if (elements.showSpit) {
        elements.showSpit.addEventListener(
            "click", () => toggleComparison("spit")
        );
    }
    if (elements.lyBasis) {
        // A different basis is a different range, so this re-fetches rather than
        // re-drawing what is already loaded.
        elements.lyBasis.addEventListener("change", refreshComparison);
    }
    // The grain decides the chart's buckets as well as the table's, so it can
    // no longer redraw the table alone.
    elements.grain.addEventListener("change", () => setGrain(elements.grain.value));
    if (elements.chartGrain) {
        elements.chartGrain.addEventListener(
            "change", () => setGrain(elements.chartGrain.value)
        );
    }
    elements.lineReset.addEventListener("click", showEveryLine);
    if (elements.chartReset) {
        elements.chartReset.addEventListener("click", resetChartView);
    }
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
    // The two selects declare the same default in the markup; this is what keeps
    // them together if one of those defaults is ever edited alone.
    if (elements.chartGrain) elements.chartGrain.value = elements.grain.value;
    syncComparisonControls();
}());
