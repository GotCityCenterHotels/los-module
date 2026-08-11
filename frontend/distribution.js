const API_BASE_URL = "/api";

const startDate = document.getElementById("startDate");
const endDate = document.getElementById("endDate");
const grain = document.getElementById("grain");
const hotelName = document.getElementById("hotelName");
const lyComparisonBasis = document.getElementById("lyComparisonBasis");
const metric = document.getElementById("metric");
const scenario = document.getElementById("scenario");
const level = document.getElementById("level");
const loadButton = document.getElementById("loadButton");
const status = document.getElementById("status");
const results = document.getElementById("distributionResults");
const errorPanel = document.getElementById("errorPanel");

const periodPicker = LosPeriodPicker.create({
    rootElement: document.getElementById("monthPicker"),
    startInput: startDate,
    endInput: endDate
});

let loadedFacts = [];
let loadedMonths = [];
let lastLoadedRequestKey = null;
let requestInProgress = false;

function isValidPeriod() {
    return Boolean(startDate.value && endDate.value && startDate.value <= endDate.value);
}

function getRequestState() {
    return {
        startDate: startDate.value,
        endDate: endDate.value,
        lyComparisonBasis: lyComparisonBasis.value,
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
        status.textContent = getRequestKey() !== lastLoadedRequestKey
            ? "Period settings changed. Click Update data to refresh."
            : "Data is up to date.";
    }
}

function validateInputs() {
    if (!startDate.value || !endDate.value) {
        throw new Error("Start date and end date are required.");
    }
    if (startDate.value > endDate.value) {
        throw new Error("Start date cannot be after end date.");
    }
}

async function loadHotels() {
    const payload = await LosApi.fetchJson(`${API_BASE_URL}/los/hotels`);
    for (const hotel of payload.data || []) {
        const option = document.createElement("option");
        option.value = hotel;
        option.textContent = hotel;
        hotelName.appendChild(option);
    }
}

async function loadData() {
    errorPanel.hidden = true;
    const requestedState = getRequestState();
    const requestedKey = JSON.stringify(requestedState);

    try {
        validateInputs();
        requestInProgress = true;
        updateLoadButtonState();
        status.textContent = "Updating LOS facts...";
        const params = new URLSearchParams({
            startDate: requestedState.startDate,
            endDate: requestedState.endDate,
            lyComparisonBasis: requestedState.lyComparisonBasis
        });
        const payload = await LosApi.fetchJson(`${API_BASE_URL}/los/facts?${params}`);
        loadedFacts = payload.data || [];
        loadedMonths = requestedState.selectedMonths;
        lastLoadedRequestKey = requestedKey;
        render();
        status.textContent = loadedFacts.length ? "Data is up to date." : "No data returned.";
    }
    catch (error) {
        console.error(error);
        errorPanel.hidden = false;
        errorPanel.textContent = error.message || "Unable to update distribution.";
        status.textContent = "Update failed.";
    }
    finally {
        requestInProgress = false;
        updateLoadButtonState();
    }
}

function render() {
    if (lastLoadedRequestKey === null) return;

    const selectedHotel = hotelName.value;
    const facts = LosData.filterByMonths(loadedFacts, loadedMonths);
    const rows = LosData.calculateDistribution(facts, {
        grain: grain.value,
        hotelCodes: selectedHotel ? [selectedHotel] : null,
        scenario: scenario.value,
        portfolio: level.value === "total",
        metric: metric.value,
        buckets: LosData.DEFAULT_LOS_BUCKETS
    });

    results.innerHTML = "";
    if (rows.length === 0) {
        results.innerHTML = '<div class="summary-card">No distribution data found.</div>';
        return;
    }
    rows.forEach(renderRow);
}

function renderRow(row) {
    const card = document.createElement("div");
    card.className = "distribution-card";
    card.innerHTML = `
        <div class="distribution-heading">
            <div><strong>${escapeHtml(row.periodKey)}</strong><span>${escapeHtml(row.hotelCode)}</span></div>
            <div>${scenarioLabel(row.scenario)} &middot; ${formatNumber(row.total)}
                ${metric.value === "bookings" ? "reservations" : "nights"}</div>
        </div>
        <div class="distribution-bar">
            ${row.values.map((item, index) => segment(`los-${Math.min(index + 1, 5)}`, item)).join("")}
        </div>
        <div class="distribution-values">
            ${row.values.map(valueItem).join("")}
        </div>`;
    results.appendChild(card);
}

function segment(cssClass, item) {
    return `<div class="distribution-segment ${cssClass}" style="width:${item.percentage}%"
        title="LOS ${escapeHtml(item.label)}: ${item.percentage.toFixed(1)}%"></div>`;
}

function valueItem(item) {
    return `<div class="distribution-value"><strong>LOS ${escapeHtml(item.label)}</strong>
        <span>${formatNumber(item.value)} &middot; ${item.percentage.toFixed(1)}%</span></div>`;
}

function scenarioLabel(value) {
    return value === "ly" ? "Actual LY" : value === "spit" ? "SPIT" : "Current";
}

function formatNumber(value) {
    return new Intl.NumberFormat("en-SE").format(value);
}

function escapeHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

startDate.addEventListener("input", markBackendSettingChanged);
endDate.addEventListener("input", markBackendSettingChanged);
document.getElementById("monthPicker").addEventListener("periodchange", markBackendSettingChanged);
lyComparisonBasis.addEventListener("change", markBackendSettingChanged);
loadButton.addEventListener("click", loadData);
grain.addEventListener("change", render);
hotelName.addEventListener("change", render);
metric.addEventListener("change", render);
scenario.addEventListener("change", render);
level.addEventListener("change", render);
document.addEventListener("DOMContentLoaded", () => {
    updateLoadButtonState();
    loadHotels().catch((error) => {
        console.error(error);
        errorPanel.hidden = false;
        errorPanel.textContent = error.message || "Unable to load hotels.";
    });
});
