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

let loadedFacts = [];
let loadedParameters = null;

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
    return getHotelCheckboxes().filter(({ checked }) => checked).map(({ value }) => value);
}

function updateHotelToggleText() {
    const checkboxes = getHotelCheckboxes();
    const selected = checkboxes.filter(({ checked }) => checked);

    if (checkboxes.length > 0 && selected.length === checkboxes.length) {
        hotelToggle.textContent = "All hotels";
    }
    else if (selected.length === 0) {
        hotelToggle.textContent = "No hotels selected";
    }
    else if (selected.length === 1) {
        hotelToggle.textContent = selected[0].value;
    }
    else {
        hotelToggle.textContent = `${selected.length} hotels selected`;
    }
}

function populateHotels(facts) {
    const existing = getHotelCheckboxes();
    const selected = new Set(existing.filter(({ checked }) => checked).map(({ value }) => value));
    const allWereSelected = existing.length === 0 || selected.size === existing.length;
    const hotels = Array.from(new Set(facts.map(({ hotelCode }) => hotelCode))).sort();

    hotelOptions.innerHTML = "";
    for (const hotel of hotels) {
        const label = document.createElement("label");
        label.className = "multi-select-option";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = hotel;
        checkbox.checked = allWereSelected || selected.has(hotel);
        checkbox.addEventListener("change", () => {
            updateHotelToggleText();
            render();
        });
        const name = document.createElement("span");
        name.textContent = hotel;
        label.append(checkbox, name);
        hotelOptions.appendChild(label);
    }

    if (hotels.length === 0) {
        hotelOptions.innerHTML = '<span class="multi-select-empty">No hotels in this dataset.</span>';
    }
    updateHotelToggleText();
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
    try {
        validateInputs();
        loadButton.disabled = true;
        loadButton.textContent = "Loading...";
        statusElement.textContent = "Loading LOS facts...";
        const params = new URLSearchParams({
            startDate: startDateInput.value,
            endDate: endDateInput.value,
            lyComparisonBasis: lyComparisonInput.value
        });
        const payload = await LosApi.fetchJson(`${API_BASE_URL}/los/facts?${params}`);
        loadedFacts = payload.data || [];
        loadedParameters = payload.parameters || null;
        populateHotels(loadedFacts);
        render();
        statusElement.textContent = loadedFacts.length ? "Data loaded." : "No data returned.";
    }
    catch (error) {
        console.error(error);
        showError(error.message || "Unable to load data.");
        statusElement.textContent = "Request failed.";
    }
    finally {
        loadButton.disabled = false;
        loadButton.textContent = "Load data";
    }
}

function render() {
    const hotels = getSelectedHotels();
    const options = { grain: grainInput.value, hotelCodes: hotels };
    const hotelRows = LosData.calculateAverageLos(loadedFacts, options);
    const totalRows = LosData.calculateAverageLos(loadedFacts, { ...options, portfolio: true });
    const rows = pivotAverageRows([...hotelRows, ...totalRows]);
    const summaryRows = LosData.calculateAverageLos(loadedFacts, {
        grain: "all",
        hotelCodes: hotels,
        portfolio: true
    });
    const summaryByScenario = Object.fromEntries(
        summaryRows.map((row) => [row.scenario, row])
    );
    const showSpit = shouldShowSpit(loadedParameters?.endDate);

    summaryElement.hidden = false;
    totalLosElement.textContent = formatDecimal(summaryByScenario.current?.averageLos ?? null);
    totalLosLyElement.textContent = formatDecimal(summaryByScenario.ly?.averageLos ?? null);
    totalLosSpitElement.textContent = formatDecimal(summaryByScenario.spit?.averageLos ?? null);
    totalLosSpitCard.hidden = !showSpit;
    renderTable(rows, showSpit);
    renderChart(rows.filter(({ hotelCode }) => hotelCode === "Total"));
}

function pivotAverageRows(rows) {
    const groups = new Map();
    for (const row of rows) {
        const key = JSON.stringify([row.periodKey, row.hotelCode]);
        const group = groups.get(key) || {
            periodKey: row.periodKey,
            hotelCode: row.hotelCode,
            scenarios: {}
        };
        group.scenarios[row.scenario] = row;
        groups.set(key, group);
    }
    return Array.from(groups.values()).sort((a, b) =>
        a.periodKey.localeCompare(b.periodKey)
        || (a.hotelCode === "Total" ? 1 : 0) - (b.hotelCode === "Total" ? 1 : 0)
        || a.hotelCode.localeCompare(b.hotelCode)
    );
}

function renderTable(rows, showSpit) {
    resultsBody.innerHTML = "";
    document.querySelector("th.los-spit-column").hidden = !showSpit;

    if (rows.length === 0) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = showSpit ? 11 : 10;
        cell.className = "empty-table-cell";
        cell.textContent = "No data returned for the selected hotels.";
        row.appendChild(cell);
        resultsBody.appendChild(row);
    }

    for (const item of rows) {
        const current = item.scenarios.current;
        const ly = item.scenarios.ly;
        const spit = item.scenarios.spit;
        const row = document.createElement("tr");
        if (item.hotelCode === "Total") row.classList.add("total-row");
        row.innerHTML = `
            <td>${escapeHtml(item.periodKey)}</td>
            <td>${escapeHtml(item.hotelCode)}</td>
            <td>${formatDecimal(current?.averageLos ?? null)}</td>
            <td>${formatDecimal(ly?.averageLos ?? null)}</td>
            <td class="los-spit-column" ${showSpit ? "" : "hidden"}>${formatDecimal(spit?.averageLos ?? null)}</td>
            <td>${formatInteger(current?.nightCount ?? null)}</td>
            <td>${formatInteger(ly?.nightCount ?? null)}</td>
            <td>${formatInteger(spit?.nightCount ?? null)}</td>
            <td>${formatInteger(current?.bookingCount ?? null)}</td>
            <td>${formatInteger(ly?.bookingCount ?? null)}</td>
            <td>${formatInteger(spit?.bookingCount ?? null)}</td>`;
        resultsBody.appendChild(row);
    }
    resultsSection.hidden = false;
}

function renderChart(rows) {
    chartSection.hidden = false;
    losChart.innerHTML = "";
    if (rows.length === 0) {
        losChart.innerHTML = '<p class="chart-empty">No portfolio data to chart.</p>';
        return;
    }

    const svgNs = "http://www.w3.org/2000/svg";
    const width = 1100;
    const height = 340;
    const margin = { top: 20, right: 24, bottom: 54, left: 58 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const series = [
        { scenario: "current", label: "LOS", color: "#2563eb" },
        { scenario: "ly", label: "LOS LY", color: "#f97316" }
    ];
    const values = rows.flatMap(({ scenarios }) =>
        series.map(({ scenario }) => scenarios[scenario]?.averageLos).filter(Number.isFinite)
    );
    if (values.length === 0) {
        losChart.innerHTML = '<p class="chart-empty">No LOS values to chart.</p>';
        return;
    }

    const maxValue = Math.max(1, Math.ceil(Math.max(...values) * 1.1));
    const x = (index) => margin.left + (rows.length === 1
        ? plotWidth / 2
        : index * plotWidth / (rows.length - 1));
    const y = (value) => margin.top + plotHeight - (value / maxValue) * plotHeight;
    const svg = document.createElementNS(svgNs, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "LOS and LOS last year over the selected period");

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

    const labelCount = Math.min(5, rows.length);
    const labelIndexes = Array.from(new Set(Array.from({ length: labelCount }, (_, index) =>
        Math.round(index * (rows.length - 1) / Math.max(1, labelCount - 1))
    )));
    for (const index of labelIndexes) {
        const label = document.createElementNS(svgNs, "text");
        label.setAttribute("x", x(index));
        label.setAttribute("y", height - 20);
        label.setAttribute("class", "chart-axis-label chart-x-label");
        label.textContent = rows[index].periodKey;
        svg.appendChild(label);
    }

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
            point.setAttribute("r", 3.5);
            point.setAttribute("fill", item.color);
            const title = document.createElementNS(svgNs, "title");
            title.textContent = `${row.periodKey}: ${item.label} ${formatDecimal(value)}`;
            point.appendChild(title);
            svg.appendChild(point);
        });
        if (pathData) {
            const path = document.createElementNS(svgNs, "path");
            path.setAttribute("d", pathData);
            path.setAttribute("fill", "none");
            path.setAttribute("stroke", item.color);
            path.setAttribute("stroke-width", "3");
            path.setAttribute("stroke-linecap", "round");
            path.setAttribute("stroke-linejoin", "round");
            svg.insertBefore(path, svg.querySelector("circle"));
        }
    }
    losChart.appendChild(svg);
}

function localToday() {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function shouldShowSpit(endDate) {
    return Boolean(endDate && endDate >= localToday());
}

function setHotelMenuOpen(open) {
    hotelMenu.hidden = !open;
    hotelToggle.setAttribute("aria-expanded", String(open));
}

function escapeHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function showError(message) {
    errorPanel.hidden = false;
    errorPanel.textContent = message;
}

function clearError() {
    errorPanel.hidden = true;
    errorPanel.textContent = "";
}

hotelToggle.addEventListener("click", () => setHotelMenuOpen(hotelMenu.hidden));
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
    if (event.key === "Escape") setHotelMenuOpen(false);
});
grainInput.addEventListener("change", render);
loadButton.addEventListener("click", loadData);
document.addEventListener("DOMContentLoaded", loadData);
