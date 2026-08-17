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
let hotelRequestId = 0;
let hotelListLoaded = false;
let loadedHotelRequestKey = null;
let loadedRequestState = null;

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
    loadButton.textContent = requestInProgress ? "Updating…" : "Update data";
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

function getHotelRequestKey(requestState) {
    return JSON.stringify([
        requestState.startDate,
        requestState.endDate,
        requestState.lyComparisonBasis
    ]);
}

async function loadHotels(requestState = loadedRequestState || getRequestState(), { forceRefresh = false } = {}) {
    const requestId = ++hotelRequestId;
    const selectedHotel = hotelName.value;
    const requestKey = getHotelRequestKey(requestState);
    if (!forceRefresh && hotelListLoaded && loadedHotelRequestKey === requestKey) return;
    if (!hotelListLoaded) hotelName.options[0].textContent = "Loading hotels…";
    const payload = await LosApi.fetchHotelList({
        apiBaseUrl: API_BASE_URL,
        startDate: requestState.startDate,
        endDate: requestState.endDate,
        lyComparisonBasis: requestState.lyComparisonBasis,
        forceRefresh
    });
    if (requestId !== hotelRequestId) return;

    hotelName.innerHTML = '<option value="">All hotels</option>';
    for (const hotel of payload.data || []) {
        const option = document.createElement("option");
        option.value = hotel;
        option.textContent = hotel;
        hotelName.appendChild(option);
    }
    if (Array.from(hotelName.options).some(({ value }) => value === selectedHotel)) {
        hotelName.value = selectedHotel;
    }
    hotelListLoaded = true;
    loadedHotelRequestKey = requestKey;
    if (lastLoadedRequestKey !== null) render();
}

async function loadData() {
    errorPanel.hidden = true;
    const requestedState = getRequestState();
    const requestedKey = JSON.stringify(requestedState);

    try {
        validateInputs();
        requestInProgress = true;
        updateLoadButtonState();
        status.textContent = "Updating…";
        const payload = await LosApi.fetchLosFactRanges({
            apiBaseUrl: API_BASE_URL,
            ...requestedState
        });
        loadedFacts = payload.data || [];
        loadedMonths = requestedState.selectedMonths;
        loadedRequestState = requestedState;
        lastLoadedRequestKey = requestedKey;
        render();
        status.textContent = loadedFacts.length
            ? "Data is up to date."
            : "No rows for this period. Try a wider date range or another hotel.";
        loadHotels(requestedState, { forceRefresh: true }).catch(handleHotelError);
    }
    catch (error) {
        console.error(error);
        errorPanel.hidden = false;
        errorPanel.textContent = error.message || "Unable to update distribution.";
        status.textContent = "Update failed.";
        // The cards have to be cleared as well as the loaded rows: every Display control calls
        // render() directly on change, so a failed update that only left the state behind would
        // repaint the previous period's distribution under the new settings.
        results.innerHTML = "";
        loadedFacts = [];
        loadedMonths = [];
        loadedRequestState = null;
        lastLoadedRequestKey = null;
    }
    finally {
        requestInProgress = false;
        updateLoadButtonState();
    }
}

function render() {
    if (lastLoadedRequestKey === null) return;

    const selectedHotel = hotelName.value;
    const rows = LosData.calculateDistribution(loadedFacts, {
        grain: grain.value,
        hotelCodes: selectedHotel ? [selectedHotel] : null,
        scenario: scenario.value,
        portfolio: level.value === "total",
        metric: metric.value,
        buckets: LosData.DEFAULT_LOS_BUCKETS,
        selectedMonths: loadedMonths
    });

    if (rows.length === 0) {
        results.innerHTML = '<div class="summary-card">No distribution data found.</div>';
        return;
    }

    const selectedGrain = grain.value;
    const unit = metric.value === "bookings" ? "reservations" : "nights";
    // A day grain across a full portfolio is a card per date per hotel, and the
    // period label is identical for every hotel within a date.
    const periodLabels = new Map();
    const markup = rows.map((row) => {
        let periodLabel = periodLabels.get(row.periodKey);
        if (periodLabel === undefined) {
            periodLabel = escapeHtml(LosFormat.periodLabel(row.periodKey, selectedGrain));
            periodLabels.set(row.periodKey, periodLabel);
        }
        return renderRow(row, periodLabel, unit);
    });

    // One parse of one string, rather than creating, filling, and inserting a
    // card element at a time into a live document.
    results.innerHTML = markup.join("");
}

function renderRow(row, periodLabel, unit) {
    return `<div class="distribution-card">
        <div class="distribution-heading">
            <div><h3>${periodLabel}</h3><span>${escapeHtml(row.hotelCode)}</span></div>
            <div>${scenarioLabel(row.scenario)} &middot; ${formatNumber(row.total)}
                ${unit}</div>
        </div>
        <div class="distribution-bar">
            ${row.values.map((item, index) => segment(`los-${Math.min(index + 1, 5)}`, item)).join("")}
        </div>
        <div class="distribution-values">
            ${row.values.map(valueItem).join("")}
        </div>
    </div>`;
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

// Constructing an Intl formatter is expensive and this is called six times per
// card, so it is built once rather than once per number.
const numberFormatter = new Intl.NumberFormat("en-SE");

function formatNumber(value) {
    return numberFormatter.format(value);
}

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => HTML_ESCAPES[character]);
}

function handleHotelError(error) {
    console.error(error);
    if (hotelName.options.length === 1) {
        hotelName.options[0].textContent = "Hotels unavailable";
    }
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
    // Same reasoning as the Average LOS page: the hotel list is small, cached
    // separately, and wanted by the first interaction, so fetching it now fills
    // the select before it is opened and warms a cold Functions instance ahead
    // of the Update data click.
    if (isValidPeriod()) loadHotels().catch(handleHotelError);
});
hotelName.addEventListener("pointerdown", () => loadHotels().catch(handleHotelError));
hotelName.addEventListener("focus", () => loadHotels().catch(handleHotelError));
