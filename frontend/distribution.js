// Wrapped, like every other module in this directory.
//
// app.js and distribution.js were the only two unwrapped page scripts, and they
// declared 27 of the same top-level names - including the whole page state
// machine, declared twice. Nothing loaded both on one page, so the collision was
// latent rather than live: the day anything did, the second file would fail to
// parse and the page would silently lose that feature. An IIFE makes it
// impossible rather than merely unlikely, and costs nothing - neither page has
// an inline handler (the CSP forbids them) and nothing outside reads these
// names.
//
// The shared state machine still lives in two copies. That is a real extraction
// and belongs in its own change; this one only closes the collision.
(function initializeLosDistributionPage() {
"use strict";

const API_BASE_URL = "/api";

const startDate = document.getElementById("startDate");
const endDate = document.getElementById("endDate");
const grain = document.getElementById("grain");
// The element keeps its id: distribution.html labels it and the <label for>
// points at it. The variable is named for what it is, so it does not read as
// the hotelName field on a fact row.
const hotelSelect = document.getElementById("hotelName");
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
// See app.js: labels read the grain the rows were rolled up to, not the live
// <select>, which disagrees with it while a grain change is in flight.
let loadedGrain = null;
let lastLoadedRequestKey = null;
let requestInProgress = false;
// A generation token, the same device supplement.js uses at state.requestId.
// loadButton.disabled was the only guard and it does not cover the Display
// controls, which call loadData()/render() directly. Two loads in flight then
// resolved last-write-wins.
let losRequestId = 0;
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
        grain: grain.value,
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
    const selectedHotel = hotelSelect.value;
    const requestKey = getHotelRequestKey(requestState);
    if (!forceRefresh && hotelListLoaded && loadedHotelRequestKey === requestKey) return;
    if (!hotelListLoaded) hotelSelect.options[0].textContent = "Loading hotels…";
    const payload = await LosApi.fetchHotelList({
        apiBaseUrl: API_BASE_URL,
        startDate: requestState.startDate,
        endDate: requestState.endDate,
        lyComparisonBasis: requestState.lyComparisonBasis,
        forceRefresh
    });
    if (requestId !== hotelRequestId) return;

    hotelSelect.innerHTML = '<option value="">All hotels</option>';
    for (const hotel of payload.data || []) {
        const option = document.createElement("option");
        option.value = hotel;
        option.textContent = hotel;
        hotelSelect.appendChild(option);
    }
    if (Array.from(hotelSelect.options).some(({ value }) => value === selectedHotel)) {
        hotelSelect.value = selectedHotel;
    }
    hotelListLoaded = true;
    loadedHotelRequestKey = requestKey;
    if (lastLoadedRequestKey !== null) render();
}

// How fresh the loaded facts are, said in words the reader can act on.
//
// The server has always known this - services/los_facts_service.py computes
// staleness on every read - and the only thing it did with the answer was write
// a log line. So these two pages reported "Data is up to date." on the strength
// of rows coming back, over a publication that could be days old. Cost Data and
// Supplement both report their own freshness; these were the exception.
//
// A null publishedAt means the raw-query fallback answered, which reads live
// source data and has no publication at all. That is worth saying too: it is a
// different mode with different performance, and nothing on either page has ever
// distinguished it.
function freshnessSuffix(payload) {
    if (!payload) return "";
    if (!payload.publishedAt) return " Read live from source.";
    const published = new Date(payload.publishedAt);
    if (Number.isNaN(published.getTime())) return "";
    const hours = Math.max(0, Math.round((Date.now() - published.getTime()) / 3600000));
    const age = hours < 1 ? "less than an hour ago"
        : hours < 24 ? `${hours} hour${hours === 1 ? "" : "s"} ago`
        : `${Math.round(hours / 24)} day${Math.round(hours / 24) === 1 ? "" : "s"} ago`;
    return payload.stale
        ? ` Published ${age} — this is behind the nightly import and may be out of date.`
        : ` Published ${age}.`;
}

async function loadData() {
    errorPanel.hidden = true;
    const requestedState = getRequestState();
    const requestedKey = JSON.stringify(requestedState);
    const requestId = ++losRequestId;

    try {
        validateInputs();
        requestInProgress = true;
        updateLoadButtonState();
        status.textContent = "Updating…";
        const payload = await LosApi.fetchLosFactRanges({
            apiBaseUrl: API_BASE_URL,
            ...requestedState
        });
        // A superseded response is dropped whole: its rows, its grain and its
        // status line all describe a request the user has moved on from.
        if (requestId !== losRequestId) return;
        loadedFacts = payload.data || [];
        loadedGrain = requestedState.grain;
        loadedRequestState = requestedState;
        lastLoadedRequestKey = requestedKey;
        render();
        status.textContent = (loadedFacts.length
            ? "Data is up to date."
            : "No rows for this period. Try a wider date range or another hotel.")
            + freshnessSuffix(payload);
        loadHotels(requestedState, { forceRefresh: true }).catch(handleHotelError);
    }
    catch (error) {
        if (requestId !== losRequestId) return;
        console.error(error);
        errorPanel.hidden = false;
        errorPanel.textContent = error.message || "Unable to update distribution.";
        status.textContent = "Update failed.";
        // The cards have to be cleared as well as the loaded rows: every Display control calls
        // render() directly on change, so a failed update that only left the state behind would
        // repaint the previous period's distribution under the new settings.
        results.innerHTML = "";
        loadedFacts = [];
        loadedGrain = null;
        loadedRequestState = null;
        lastLoadedRequestKey = null;
    }
    finally {
        // Only the newest request owns the button.
        if (requestId === losRequestId) {
            requestInProgress = false;
            updateLoadButtonState();
        }
    }
}

function render() {
    if (lastLoadedRequestKey === null) return;

    const selectedHotel = hotelSelect.value;
    // No selectedMonths: the request already covered exactly the selected
    // months, and a server-rolled week bucket carries its Monday, which can fall
    // in the month before the one asked for. See app.js.
    const rows = LosData.calculateDistribution(loadedFacts, {
        grain: loadedGrain,
        hotelNames: selectedHotel ? [selectedHotel] : null,
        scenario: scenario.value,
        portfolio: level.value === "total",
        metric: metric.value,
        buckets: LosData.DEFAULT_LOS_BUCKETS
    });

    if (rows.length === 0) {
        results.innerHTML = '<div class="summary-card">No distribution data found.</div>';
        return;
    }

    const selectedGrain = loadedGrain;
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
            <div><h3>${periodLabel}</h3><span>${escapeHtml(row.hotelName)}</span></div>
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
    if (hotelSelect.options.length === 1) {
        hotelSelect.options[0].textContent = "Hotels unavailable";
    }
}

startDate.addEventListener("input", markBackendSettingChanged);
endDate.addEventListener("input", markBackendSettingChanged);
document.getElementById("monthPicker").addEventListener("periodchange", markBackendSettingChanged);
lyComparisonBasis.addEventListener("change", markBackendSettingChanged);
loadButton.addEventListener("click", loadData);
// Part of the request now, not a local repaint - see app.js.
grain.addEventListener("change", () => {
    if (lastLoadedRequestKey === null) {
        updateLoadButtonState();
        return;
    }
    loadData();
});
hotelSelect.addEventListener("change", render);
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
hotelSelect.addEventListener("pointerdown", () => loadHotels().catch(handleHotelError));
hotelSelect.addEventListener("focus", () => loadHotels().catch(handleHotelError));

}());
