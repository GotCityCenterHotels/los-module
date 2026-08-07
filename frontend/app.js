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
let hotelListLoaded = false;

const decimalFormatter = new Intl.NumberFormat("en-SE", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
});

const integerFormatter = new Intl.NumberFormat("en-SE", {
    maximumFractionDigits: 0
});

function formatDecimal(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
        return "-";
    }

    return decimalFormatter.format(Number(value));
}

function formatInteger(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
        return "-";
    }

    return integerFormatter.format(Number(value));
}

function toFiniteNumber(value) {
    if (value === null || value === undefined || value === "") {
        return null;
    }

    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

function getHotelCheckboxes() {
    return Array.from(hotelOptions.querySelectorAll('input[type="checkbox"]'));
}

function getSelectedHotels() {
    return getHotelCheckboxes()
        .filter((checkbox) => checkbox.checked)
        .map((checkbox) => checkbox.value);
}

function updateHotelToggleText() {
    const checkboxes = getHotelCheckboxes();
    const selectedCount = checkboxes.filter((checkbox) => checkbox.checked).length;

    if (checkboxes.length > 0 && selectedCount === checkboxes.length) {
        hotelToggle.textContent = "All hotels";
    }
    else if (selectedCount === 0) {
        hotelToggle.textContent = "No hotels selected";
    }
    else if (selectedCount === 1) {
        hotelToggle.textContent = checkboxes.find((checkbox) => checkbox.checked).value;
    }
    else {
        hotelToggle.textContent = `${selectedCount} hotels selected`;
    }
}

function setHotelMenuOpen(isOpen) {
    hotelMenu.hidden = !isOpen;
    hotelToggle.setAttribute("aria-expanded", String(isOpen));
}

async function loadHotels() {
    const response = await fetch(`${API_BASE_URL}/los/hotels`);
    let result;

    try {
        result = await response.json();
    }
    catch {
        throw new Error("The hotel list did not return valid JSON.");
    }

    if (!response.ok) {
        throw new Error(result.error || "Unable to load hotels.");
    }

    hotelOptions.innerHTML = "";

    for (const hotel of result.data || []) {
        const label = document.createElement("label");
        label.className = "multi-select-option";

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = hotel;
        checkbox.checked = true;
        checkbox.addEventListener("change", updateHotelToggleText);

        const name = document.createElement("span");
        name.textContent = hotel;

        label.append(checkbox, name);
        hotelOptions.appendChild(label);
    }

    if ((result.data || []).length === 0) {
        hotelOptions.innerHTML = '<span class="multi-select-empty">No hotels found.</span>';
    }

    hotelListLoaded = true;
    updateHotelToggleText();
}

function buildRequestUrl() {
    const params = new URLSearchParams();
    params.set("startDate", startDateInput.value);
    params.set("endDate", endDateInput.value);
    params.set("grain", grainInput.value);
    params.set("lyComparisonBasis", lyComparisonInput.value);
    if (hotelListLoaded) {
        params.set("hotelNames", JSON.stringify(getSelectedHotels()));
    }

    return `${API_BASE_URL}/los/average?${params.toString()}`;
}

function validateInputs() {
    if (!startDateInput.value) {
        throw new Error("Start date is required.");
    }

    if (!endDateInput.value) {
        throw new Error("End date is required.");
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
        statusElement.textContent = "Loading LOS data...";

        const response = await fetch(buildRequestUrl());
        let result;

        try {
            result = await response.json();
        }
        catch {
            throw new Error(`API returned HTTP ${response.status} but did not return JSON.`);
        }

        if (!response.ok) {
            throw new Error(result.error || `API request failed with HTTP ${response.status}`);
        }

        const rows = result.data || [];
        const showSpit = shouldShowSpit(result.parameters?.endDate);

        renderSummary(rows, showSpit);
        renderChart(rows);
        renderTable(rows, showSpit);
        statusElement.textContent = rows.length ? "Data loaded." : "No data returned.";
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

function getPortfolioRows(rows) {
    return rows.filter((row) => row.hotel_code === "Total");
}

function calculatePortfolioLos(rows, roomNightsKey, reservationsKey) {
    let roomNights = 0;
    let reservations = 0;

    for (const row of getPortfolioRows(rows)) {
        const rowRoomNights = toFiniteNumber(row[roomNightsKey]);
        const rowReservations = toFiniteNumber(row[reservationsKey]);

        if (rowRoomNights !== null && rowReservations !== null) {
            roomNights += rowRoomNights;
            reservations += rowReservations;
        }
    }

    return reservations > 0 ? roomNights / reservations : null;
}

function renderSummary(rows, showSpit) {
    summaryElement.hidden = false;
    totalLosElement.textContent = formatDecimal(
        calculatePortfolioLos(rows, "rn", "total_bookings")
    );
    totalLosLyElement.textContent = formatDecimal(
        calculatePortfolioLos(rows, "rnly", "total_bookings_ly")
    );
    totalLosSpitElement.textContent = formatDecimal(
        calculatePortfolioLos(rows, "spit_rn_non_strict_arrival", "total_bookings_spit")
    );
    totalLosSpitCard.hidden = !showSpit;
}

function localToday() {
    const today = new Date();
    const year = today.getFullYear();
    const month = String(today.getMonth() + 1).padStart(2, "0");
    const day = String(today.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
}

function shouldShowSpit(endDate) {
    return Boolean(endDate && endDate >= localToday());
}

function renderTable(rows, showSpit) {
    resultsBody.innerHTML = "";
    document.querySelector("th.los-spit-column").hidden = !showSpit;

    if (rows.length === 0) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = showSpit ? 11 : 10;
        cell.className = "empty-table-cell";
        cell.textContent = "No data returned.";
        row.appendChild(cell);
        resultsBody.appendChild(row);
        resultsSection.hidden = false;
        return;
    }

    for (const item of rows) {
        const row = document.createElement("tr");

        if (item.hotel_code === "Total") {
            row.classList.add("total-row");
        }

        row.innerHTML = `
            <td>${escapeHtml(item.bucket_date)}</td>
            <td>${escapeHtml(item.hotel_code)}</td>
            <td>${formatDecimal(item.los)}</td>
            <td>${formatDecimal(item.losly)}</td>
            <td class="los-spit-column" ${showSpit ? "" : "hidden"}>${formatDecimal(item.spit_los_non_strict_arrival)}</td>
            <td>${formatInteger(item.rn)}</td>
            <td>${formatInteger(item.rnly)}</td>
            <td>${formatInteger(item.spit_rn_non_strict_arrival)}</td>
            <td>${formatInteger(item.total_bookings)}</td>
            <td>${formatInteger(item.total_bookings_ly)}</td>
            <td>${formatInteger(item.total_bookings_spit)}</td>
        `;

        resultsBody.appendChild(row);
    }

    resultsSection.hidden = false;
}

function renderChart(rows) {
    const chartRows = getPortfolioRows(rows)
        .slice()
        .sort((a, b) => String(a.bucket_date).localeCompare(String(b.bucket_date)));

    chartSection.hidden = false;
    losChart.innerHTML = "";

    if (chartRows.length === 0) {
        losChart.innerHTML = '<p class="chart-empty">No portfolio data to chart.</p>';
        return;
    }

    const series = [
        { key: "los", label: "LOS", color: "#2563eb" },
        { key: "losly", label: "LOS LY", color: "#f97316" }
    ];
    const values = chartRows.flatMap((row) =>
        series.map(({ key }) => toFiniteNumber(row[key])).filter((value) => value !== null)
    );

    if (values.length === 0) {
        losChart.innerHTML = '<p class="chart-empty">No LOS values to chart.</p>';
        return;
    }

    const svgNamespace = "http://www.w3.org/2000/svg";
    const width = 1100;
    const height = 340;
    const margin = { top: 20, right: 24, bottom: 54, left: 58 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const maxValue = Math.max(1, Math.ceil(Math.max(...values) * 1.1));
    const x = (index) => margin.left + (chartRows.length === 1
        ? plotWidth / 2
        : index * plotWidth / (chartRows.length - 1));
    const y = (value) => margin.top + plotHeight - (value / maxValue) * plotHeight;
    const svg = document.createElementNS(svgNamespace, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "LOS and LOS last year over the selected period");

    for (let tick = 0; tick <= 4; tick += 1) {
        const tickValue = maxValue * tick / 4;
        const tickY = y(tickValue);
        const gridLine = document.createElementNS(svgNamespace, "line");
        gridLine.setAttribute("x1", margin.left);
        gridLine.setAttribute("x2", width - margin.right);
        gridLine.setAttribute("y1", tickY);
        gridLine.setAttribute("y2", tickY);
        gridLine.setAttribute("class", "chart-grid-line");
        svg.appendChild(gridLine);

        const label = document.createElementNS(svgNamespace, "text");
        label.setAttribute("x", margin.left - 12);
        label.setAttribute("y", tickY + 4);
        label.setAttribute("class", "chart-axis-label chart-y-label");
        label.textContent = tickValue.toFixed(1);
        svg.appendChild(label);
    }

    const labelIndexes = Array.from(new Set(
        Array.from({ length: Math.min(5, chartRows.length) }, (_, index) =>
            Math.round(index * (chartRows.length - 1) / Math.max(1, Math.min(5, chartRows.length) - 1))
        )
    ));

    for (const index of labelIndexes) {
        const label = document.createElementNS(svgNamespace, "text");
        label.setAttribute("x", x(index));
        label.setAttribute("y", height - 20);
        label.setAttribute("class", "chart-axis-label chart-x-label");
        label.textContent = chartRows[index].bucket_date;
        svg.appendChild(label);
    }

    for (const item of series) {
        let pathData = "";
        let lastPointWasPresent = false;

        chartRows.forEach((row, index) => {
            const value = toFiniteNumber(row[item.key]);
            if (value === null) {
                lastPointWasPresent = false;
                return;
            }

            pathData += `${lastPointWasPresent ? " L" : "M"} ${x(index)} ${y(value)}`;
            lastPointWasPresent = true;

            const point = document.createElementNS(svgNamespace, "circle");
            point.setAttribute("cx", x(index));
            point.setAttribute("cy", y(value));
            point.setAttribute("r", 3.5);
            point.setAttribute("fill", item.color);
            const title = document.createElementNS(svgNamespace, "title");
            title.textContent = `${row.bucket_date}: ${item.label} ${formatDecimal(value)}`;
            point.appendChild(title);
            svg.appendChild(point);
        });

        if (pathData) {
            const path = document.createElementNS(svgNamespace, "path");
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

function escapeHtml(value) {
    if (value === null || value === undefined) {
        return "";
    }

    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
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
});

clearAllHotelsButton.addEventListener("click", () => {
    getHotelCheckboxes().forEach((checkbox) => { checkbox.checked = false; });
    updateHotelToggleText();
});

document.addEventListener("click", (event) => {
    if (!hotelSelect.contains(event.target)) {
        setHotelMenuOpen(false);
    }
});

document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
        setHotelMenuOpen(false);
        hotelToggle.focus();
    }
});

loadButton.addEventListener("click", loadData);

document.addEventListener("DOMContentLoaded", async () => {
    try {
        await loadHotels();
    }
    catch (error) {
        console.error(error);
        hotelOptions.innerHTML = '<span class="multi-select-empty">Hotel list unavailable.</span>';
        showError(error.message || "Unable to load hotels.");
    }

    loadData();
});
