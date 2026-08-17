const API_BASE_URL = "/api";

const startDateInput = document.getElementById("startDate");
const endDateInput = document.getElementById("endDate");
const grainInput = document.getElementById("grain");
const lyComparisonInput = document.getElementById("lyComparisonBasis");
const loadButton = document.getElementById("loadButton");
const statusElement = document.getElementById("status");
const summaryElement = document.getElementById("summary");
const totalLosElement = document.getElementById("totalLos");
const totalLosLyElement = document.getElementById("totalLosLy");
const totalLosSpitElement = document.getElementById("totalLosSpit");
const totalLosSpitCard = document.getElementById("totalLosSpitCard");
const chartSection = document.getElementById("chartSection");
const losChart = document.getElementById("losChart");
const resultsSection = document.getElementById("resultsSection");
const resultsBody = document.getElementById("resultsBody");
const errorPanel = document.getElementById("errorPanel");
const hotelSelect = document.getElementById("hotelSelect");
const hotelToggle = document.getElementById("hotelToggle");
const hotelMenu = document.getElementById("hotelMenu");
const hotelOptions = document.getElementById("hotelOptions");
const selectAllHotelsButton = document.getElementById("selectAllHotels");
const clearAllHotelsButton = document.getElementById("clearAllHotels");

const periodPicker = LosPeriodPicker.create({
    rootElement: document.getElementById("monthPicker"),
    startInput: startDateInput,
    endInput: endDateInput
});

let loadedFacts = [];
let loadedMonths = [];
let lastLoadedRequestKey = null;
let hotelListLoaded = false;
let requestInProgress = false;
let hotelRequestId = 0;
let loadedHotelRequestKey = null;
let loadedRequestState = null;

const decimalFormatter = new Intl.NumberFormat("en-SE", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
});
const integerFormatter = new Intl.NumberFormat("en-SE", { maximumFractionDigits: 0 });

function formatDecimal(value) {
    return value === null || !Number.isFinite(Number(value))
        ? "-"
        : decimalFormatter.format(Number(value));
}

function formatInteger(value) {
    return value === null || !Number.isFinite(Number(value))
        ? "-"
        : integerFormatter.format(Number(value));
}

function getHotelCheckboxes() {
    return Array.from(hotelOptions.querySelectorAll('input[type="checkbox"]'));
}

function getSelectedHotels() {
    if (!hotelListLoaded) return null;
    return getHotelCheckboxes().filter(({ checked }) => checked).map(({ value }) => value);
}

function updateHotelToggleText() {
    const checkboxes = getHotelCheckboxes();
    const selected = checkboxes.filter(({ checked }) => checked);

    if (!hotelListLoaded) hotelToggle.textContent = "All hotels";
    else if (checkboxes.length > 0 && selected.length === checkboxes.length) hotelToggle.textContent = "All hotels";
    else if (selected.length === 0) hotelToggle.textContent = "No hotels selected";
    else if (selected.length === 1) hotelToggle.textContent = selected[0].value;
    else hotelToggle.textContent = `${selected.length} hotels selected`;

    // Only the single-hotel branch writes text the button cannot hold - most
    // GCCH names run past the ~26 characters that fit - so that is the one
    // branch that needs a tooltip carrying the full name.
    if (hotelListLoaded && selected.length === 1) hotelToggle.title = selected[0].value;
    else hotelToggle.removeAttribute("title");
}

function getHotelRequestKey(requestState) {
    return JSON.stringify([
        requestState.startDate,
        requestState.endDate,
        requestState.lyComparisonBasis
    ]);
}

async function loadHotels(requestState = loadedRequestState || getRequestState(), { forceRefresh = false } = {}) {
    const requestId = ++hotelRequestId;
    const existing = getHotelCheckboxes();
    const selectedHotels = new Set(existing.filter(({ checked }) => checked).map(({ value }) => value));
    const allWereSelected = !hotelListLoaded || selectedHotels.size === existing.length;
    const requestKey = getHotelRequestKey(requestState);
    if (!forceRefresh && hotelListLoaded && loadedHotelRequestKey === requestKey) return;
    if (!hotelListLoaded) {
        hotelToggle.textContent = "Loading hotels…";
        hotelOptions.innerHTML = '<span class="multi-select-empty">Loading hotels…</span>';
    }
    const payload = await LosApi.fetchHotelList({
        apiBaseUrl: API_BASE_URL,
        startDate: requestState.startDate,
        endDate: requestState.endDate,
        lyComparisonBasis: requestState.lyComparisonBasis,
        forceRefresh
    });
    if (requestId !== hotelRequestId) return;

    hotelOptions.innerHTML = "";

    for (const hotel of payload.data || []) {
        const label = document.createElement("label");
        label.className = "multi-select-option";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = hotel;
        checkbox.checked = allWereSelected || selectedHotels.has(hotel);
        checkbox.addEventListener("change", () => {
            updateHotelToggleText();
            render();
        });
        const name = document.createElement("span");
        name.textContent = hotel;
        label.append(checkbox, name);
        hotelOptions.appendChild(label);
    }

    hotelListLoaded = true;
    loadedHotelRequestKey = requestKey;
    if ((payload.data || []).length === 0) {
        hotelOptions.innerHTML = '<span class="multi-select-empty">No hotels found.</span>';
    }
    updateHotelToggleText();
    if (lastLoadedRequestKey !== null) render();
}

function isValidPeriod() {
    return Boolean(
        startDateInput.value
        && endDateInput.value
        && startDateInput.value <= endDateInput.value
    );
}

function getRequestState() {
    return {
        startDate: startDateInput.value,
        endDate: endDateInput.value,
        lyComparisonBasis: lyComparisonInput.value,
        selectedMonths: periodPicker.getSelectedMonths()
    };
}

function getRequestKey() {
    return JSON.stringify(getRequestState());
}

function updateLoadButtonState() {
    const changed = lastLoadedRequestKey === null || getRequestKey() !== lastLoadedRequestKey;
    loadButton.disabled = requestInProgress || !isValidPeriod() || !changed;
    loadButton.textContent = requestInProgress ? "Updating..." : "Update data";
}

function markBackendSettingChanged() {
    updateLoadButtonState();
    if (lastLoadedRequestKey !== null) {
        statusElement.textContent = getRequestKey() !== lastLoadedRequestKey
            ? "Period settings changed. Click Update data to refresh."
            : "Data is up to date.";
    }
}

function validateInputs() {
    if (!startDateInput.value || !endDateInput.value) {
        throw new Error("Start date and end date are required.");
    }
    if (startDateInput.value > endDateInput.value) {
        throw new Error("Start date cannot be after end date.");
    }
}

async function loadData() {
    clearError();
    const requestedState = getRequestState();
    const requestedKey = JSON.stringify(requestedState);

    try {
        validateInputs();
        requestInProgress = true;
        updateLoadButtonState();
        statusElement.textContent = "Updating…";
        const payload = await LosApi.fetchLosFactRanges({
            apiBaseUrl: API_BASE_URL,
            ...requestedState
        });
        loadedFacts = payload.data || [];
        loadedMonths = requestedState.selectedMonths;
        loadedRequestState = requestedState;
        lastLoadedRequestKey = requestedKey;
        render();
        statusElement.textContent = loadedFacts.length
            ? "Data is up to date."
            : "No rows for this period. Try a wider date range or another hotel.";
        loadHotels(requestedState, { forceRefresh: true }).catch(handleHotelError);
    }
    catch (error) {
        console.error(error);
        showError(error.message || "Unable to update data.");
        statusElement.textContent = "Update failed.";
        summaryElement.hidden = true;
        chartSection.hidden = true;
        resultsSection.hidden = true;
        // Hiding is not enough: the Group by handler calls render() directly,
        // which unhides all three sections again and repaints whatever is still
        // in loadedFacts - the numbers from the range we just failed to replace.
        loadedFacts = [];
        loadedRequestState = null;
    }
    finally {
        requestInProgress = false;
        updateLoadButtonState();
    }
}

function render() {
    if (lastLoadedRequestKey === null) return;

    const hotels = getSelectedHotels();
    const view = LosData.calculateAverageView(loadedFacts, {
        grain: grainInput.value,
        hotelCodes: hotels,
        selectedMonths: loadedMonths
    });
    const rows = pivotAverageRows(view.rows);
    const summaryByScenario = Object.fromEntries(view.summaryRows.map((row) => [row.scenario, row]));
    summaryElement.hidden = false;
    totalLosElement.textContent = formatDecimal(summaryByScenario.current?.averageLos ?? null);
    totalLosLyElement.textContent = formatDecimal(summaryByScenario.ly?.averageLos ?? null);
    totalLosSpitElement.textContent = formatDecimal(summaryByScenario.spit?.averageLos ?? null);
    totalLosSpitCard.hidden = false;
    renderTable(rows);
    renderChart(rows.filter(({ hotelCode }) => hotelCode === "Total"));
}

const collator = new Intl.Collator();

function pivotAverageRows(rows) {
    // Nested lookups rather than one composite key string per row: the keys are
    // built fresh every render and the engine has to hash each one end to end,
    // where the strings the rows already carry are hashed once and cached.
    const byPeriod = new Map();
    const pivoted = [];

    for (const row of rows) {
        let byHotel = byPeriod.get(row.periodKey);
        if (byHotel === undefined) {
            byHotel = new Map();
            byPeriod.set(row.periodKey, byHotel);
        }
        let group = byHotel.get(row.hotelCode);
        if (group === undefined) {
            group = { periodKey: row.periodKey, hotelCode: row.hotelCode, scenarios: {} };
            byHotel.set(row.hotelCode, group);
            pivoted.push(group);
        }
        group.scenarios[row.scenario] = row;
    }

    return pivoted.sort((a, b) =>
        (a.periodKey < b.periodKey ? -1 : a.periodKey > b.periodKey ? 1 : 0)
        || (a.hotelCode === "Total" ? 1 : 0) - (b.hotelCode === "Total" ? 1 : 0)
        || collator.compare(a.hotelCode, b.hotelCode)
    );
}

function renderTable(rows) {
    document.querySelector("th.los-spit-column").hidden = false;

    if (rows.length === 0) {
        resultsBody.innerHTML =
            '<tr><td colspan="11" class="empty-table-cell">No rows for the selected hotels and period.</td></tr>';
        resultsSection.hidden = false;
        return;
    }

    const grain = grainInput.value;
    // A day grain across a full portfolio is a row per date per hotel, and the
    // period label repeats once per hotel within each date.
    const periodLabels = new Map();
    const markup = new Array(rows.length);

    for (let index = 0; index < rows.length; index += 1) {
        const item = rows[index];
        const current = item.scenarios.current;
        const ly = item.scenarios.ly;
        const spit = item.scenarios.spit;
        let periodLabel = periodLabels.get(item.periodKey);
        if (periodLabel === undefined) {
            periodLabel = escapeHtml(LosFormat.periodLabel(item.periodKey, grain));
            periodLabels.set(item.periodKey, periodLabel);
        }

        markup[index] = `<tr${item.hotelCode === "Total" ? ' class="total-row"' : ""}>`
            + `<td>${periodLabel}</td>`
            + `<td>${escapeHtml(item.hotelCode)}</td>`
            + `<td>${formatDecimal(current?.averageLos ?? null)}</td>`
            + `<td>${formatDecimal(ly?.averageLos ?? null)}</td>`
            + `<td class="los-spit-column">${formatDecimal(spit?.averageLos ?? null)}</td>`
            + `<td>${formatInteger(current?.nightCount ?? null)}</td>`
            + `<td>${formatInteger(ly?.nightCount ?? null)}</td>`
            + `<td>${formatInteger(spit?.nightCount ?? null)}</td>`
            + `<td>${formatInteger(current?.bookingCount ?? null)}</td>`
            + `<td>${formatInteger(ly?.bookingCount ?? null)}</td>`
            + `<td>${formatInteger(spit?.bookingCount ?? null)}</td></tr>`;
    }

    // One parse of one string. Assigning innerHTML per row made the browser
    // parse, build, and insert a fragment thousands of times over, each one
    // touching a live table.
    resultsBody.innerHTML = markup.join("");
    resultsSection.hidden = false;
}

// Month and year buckets carry their own year in the label ("Jan 2026"), so
// they return no separate year band - it would only repeat what is already
// under the tick. Day and week buckets still band the year below the axis.
function getChartLabel(periodKey) {
    return LosFormat.periodLabelParts(periodKey, grainInput.value);
}

function tooltipScenario(label, color, row) {
    return `
        <div class="chart-tooltip-series">
            <span><i style="background:${color}"></i>${label}</span>
            <strong>${formatDecimal(row?.averageLos ?? null)}</strong>
            <small>${formatInteger(row?.nightCount ?? null)} nights &middot; ${formatInteger(row?.bookingCount ?? null)} reservations</small>
        </div>`;
}

function renderChart(rows) {
    chartSection.hidden = false;
    losChart.innerHTML = "";
    if (rows.length === 0) {
        losChart.innerHTML = '<p class="chart-empty">Nothing to chart yet — try a wider date range.</p>';
        return;
    }

    const svgNs = "http://www.w3.org/2000/svg";
    const width = 1100;
    const height = 370;
    const margin = { top: 24, right: 24, bottom: 78, left: 58 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const series = [
        { scenario: "current", label: "LOS", color: "#2563eb" },
        { scenario: "ly", label: "LOS LY", color: "#f97316" },
        { scenario: "spit", label: "LOS SPIT", color: "#7c3aed" }
    ];
    const values = rows.flatMap(({ scenarios }) =>
        series.map(({ scenario }) => scenarios[scenario]?.averageLos).filter(Number.isFinite)
    );
    if (values.length === 0) {
        losChart.innerHTML = '<p class="chart-empty">No LOS values to chart.</p>';
        return;
    }

    const maxValue = Math.max(1, Math.ceil(Math.max(...values) * 1.1));
    const x = (index) => margin.left + (rows.length === 1 ? plotWidth / 2 : index * plotWidth / (rows.length - 1));
    const y = (value) => margin.top + plotHeight - (value / maxValue) * plotHeight;
    const svg = document.createElementNS(svgNs, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Current, last year, and SPIT LOS over the selected period");

    for (let tick = 0; tick <= 4; tick += 1) {
        const value = maxValue * tick / 4;
        const line = document.createElementNS(svgNs, "line");
        line.setAttribute("x1", margin.left);
        line.setAttribute("x2", width - margin.right);
        line.setAttribute("y1", y(value));
        line.setAttribute("y2", y(value));
        line.setAttribute("class", "chart-grid-line");
        svg.appendChild(line);
        const label = document.createElementNS(svgNs, "text");
        label.setAttribute("x", margin.left - 12);
        label.setAttribute("y", y(value) + 4);
        label.setAttribute("class", "chart-axis-label chart-y-label");
        label.textContent = value.toFixed(1);
        svg.appendChild(label);
    }

    // "Jan 2026" is roughly twice as wide as the old "JAN", so a full year of
    // monthly ticks still fits but anything longer has to thin out.
    const labelStep = grainInput.value === "month" && rows.length <= 12
        ? 1
        : Math.max(1, Math.ceil(rows.length / 10));
    rows.forEach((row, index) => {
        if (index % labelStep !== 0 && index !== rows.length - 1) return;
        const parts = getChartLabel(row.periodKey);
        const label = document.createElementNS(svgNs, "text");
        label.setAttribute("x", x(index));
        label.setAttribute("y", height - 38);
        label.setAttribute("class", "chart-axis-label chart-x-label chart-period-label");
        label.textContent = parts.primary;
        svg.appendChild(label);
    });

    if (getChartLabel(rows[0].periodKey).year !== null) {
        const yearGroups = [];
        rows.forEach((row, index) => {
            const year = getChartLabel(row.periodKey).year;
            const last = yearGroups.at(-1);
            if (last?.year === year) last.end = index;
            else yearGroups.push({ year, start: index, end: index });
        });
        yearGroups.forEach(({ year, start, end }) => {
            const label = document.createElementNS(svgNs, "text");
            label.setAttribute("x", (x(start) + x(end)) / 2);
            label.setAttribute("y", height - 14);
            label.setAttribute("class", "chart-axis-label chart-x-label chart-year-label");
            label.textContent = year;
            svg.appendChild(label);
        });
    }

    const pointLayer = document.createElementNS(svgNs, "g");
    for (const item of series) {
        let pathData = "";
        let previousPresent = false;
        rows.forEach((row, index) => {
            const value = row.scenarios[item.scenario]?.averageLos;
            if (!Number.isFinite(value)) {
                previousPresent = false;
                return;
            }
            pathData += `${previousPresent ? " L" : "M"} ${x(index)} ${y(value)}`;
            previousPresent = true;
            const point = document.createElementNS(svgNs, "circle");
            point.setAttribute("cx", x(index));
            point.setAttribute("cy", y(value));
            point.setAttribute("r", 4);
            point.setAttribute("fill", item.color);
            point.setAttribute("class", "chart-point");
            pointLayer.appendChild(point);
        });
        if (pathData) {
            const path = document.createElementNS(svgNs, "path");
            path.setAttribute("d", pathData);
            path.setAttribute("fill", "none");
            path.setAttribute("stroke", item.color);
            path.setAttribute("stroke-width", "3");
            path.setAttribute("stroke-linecap", "round");
            path.setAttribute("stroke-linejoin", "round");
            svg.appendChild(path);
        }
    }
    svg.appendChild(pointLayer);

    const hoverLine = document.createElementNS(svgNs, "line");
    hoverLine.setAttribute("y1", margin.top);
    hoverLine.setAttribute("y2", margin.top + plotHeight);
    hoverLine.setAttribute("class", "chart-hover-line");
    hoverLine.setAttribute("visibility", "hidden");
    svg.appendChild(hoverLine);

    const tooltip = document.createElement("div");
    tooltip.className = "chart-tooltip";
    tooltip.hidden = true;

    function showPoint(index) {
        const row = rows[index];
        const label = getChartLabel(row.periodKey);
        hoverLine.setAttribute("visibility", "visible");
        hoverLine.setAttribute("x1", x(index));
        hoverLine.setAttribute("x2", x(index));
        tooltip.hidden = false;
        tooltip.style.left = `${x(index) / width * 100}%`;
        tooltip.innerHTML = `
            <strong class="chart-tooltip-title">${escapeHtml(label.primary)}${label.year ? ` ${escapeHtml(label.year)}` : ""}</strong>
            ${tooltipScenario("LOS", "#2563eb", row.scenarios.current)}
            ${tooltipScenario("LOS LY", "#f97316", row.scenarios.ly)}
            ${tooltipScenario("LOS SPIT", "#7c3aed", row.scenarios.spit)}`;

        // The flip has to be measured, and only once the content is in. x() is
        // in viewBox units (1100 wide) while the tooltip is sized in CSS pixels,
        // so a fixed viewBox fraction overhangs further the narrower the panel
        // gets - and .chart-panel { overflow: hidden } amputates it mid-word
        // rather than scrolling.
        const hostWidth = losChart.clientWidth;
        const leftPx = x(index) / width * hostWidth;
        tooltip.classList.toggle("align-right", leftPx + 14 + tooltip.offsetWidth > hostWidth);
    }

    function hidePoint() {
        hoverLine.setAttribute("visibility", "hidden");
        tooltip.hidden = true;
    }

    rows.forEach((row, index) => {
        const previousX = index === 0 ? margin.left : (x(index - 1) + x(index)) / 2;
        const nextX = index === rows.length - 1 ? width - margin.right : (x(index) + x(index + 1)) / 2;
        const hitArea = document.createElementNS(svgNs, "rect");
        hitArea.setAttribute("x", previousX);
        hitArea.setAttribute("y", margin.top);
        hitArea.setAttribute("width", Math.max(1, nextX - previousX));
        hitArea.setAttribute("height", plotHeight);
        hitArea.setAttribute("class", "chart-hit-area");
        hitArea.setAttribute("tabindex", "0");
        hitArea.setAttribute(
            "aria-label",
            `Show data for ${LosFormat.periodLabel(row.periodKey, grainInput.value)}`
        );
        hitArea.addEventListener("mouseenter", () => showPoint(index));
        hitArea.addEventListener("focus", () => showPoint(index));
        hitArea.addEventListener("mouseleave", hidePoint);
        hitArea.addEventListener("blur", hidePoint);
        svg.appendChild(hitArea);
    });

    losChart.append(svg, tooltip);
}

function setHotelMenuOpen(open) {
    hotelMenu.hidden = !open;
    hotelToggle.setAttribute("aria-expanded", String(open));
}

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };

// One pass instead of five. This runs twice per table row, so on a day-grain
// year it is thousands of full string rewrites either way.
function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => HTML_ESCAPES[character]);
}

function showError(message) {
    errorPanel.hidden = false;
    errorPanel.textContent = message;
}

function clearError() {
    errorPanel.hidden = true;
    errorPanel.textContent = "";
}

function handleHotelError(error) {
    console.error(error);
    if (!hotelListLoaded) {
        hotelOptions.innerHTML = '<span class="multi-select-empty">Hotel list unavailable.</span>';
        hotelToggle.textContent = "Hotels unavailable";
    }
}

hotelToggle.addEventListener("click", () => {
    const opening = hotelMenu.hidden;
    setHotelMenuOpen(opening);
    if (opening) loadHotels().catch(handleHotelError);
});
selectAllHotelsButton.addEventListener("click", () => {
    getHotelCheckboxes().forEach((checkbox) => { checkbox.checked = true; });
    updateHotelToggleText();
    render();
});
clearAllHotelsButton.addEventListener("click", () => {
    getHotelCheckboxes().forEach((checkbox) => { checkbox.checked = false; });
    updateHotelToggleText();
    render();
});
document.addEventListener("click", (event) => {
    if (!hotelSelect.contains(event.target)) setHotelMenuOpen(false);
});
document.addEventListener("keydown", (event) => {
    // Closing the menu sets [hidden], which takes the focused checkbox out of
    // layout and drops focus to <body>, so the toggle has to take it back. The
    // menu-state guard keeps Escape from stealing focus when nothing is open.
    if (event.key === "Escape" && !hotelMenu.hidden) {
        setHotelMenuOpen(false);
        hotelToggle.focus();
    }
});
startDateInput.addEventListener("input", markBackendSettingChanged);
endDateInput.addEventListener("input", markBackendSettingChanged);
document.getElementById("monthPicker").addEventListener("periodchange", markBackendSettingChanged);
lyComparisonInput.addEventListener("change", markBackendSettingChanged);
grainInput.addEventListener("change", render);
loadButton.addEventListener("click", loadData);
document.addEventListener("DOMContentLoaded", () => {
    updateHotelToggleText();
    updateLoadButtonState();
    // Nothing on the page waits for the hotel list, and it is small, separately
    // cached, and needed by the first interaction either way. Starting it now
    // means the dropdown is already populated when it is opened, and it wakes a
    // cold Functions instance so the Update data click behind it does not also
    // pay for the start-up.
    if (isValidPeriod()) loadHotels().catch(handleHotelError);
});
