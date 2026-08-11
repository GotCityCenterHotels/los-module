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

let loadedFacts = [];

function validateInputs() {
    if (!startDate.value || !endDate.value) {
        throw new Error("Start date and end date are required.");
    }
    if (startDate.value > endDate.value) {
        throw new Error("Start date cannot be after end date.");
    }
}

async function loadData() {
    errorPanel.hidden = true;
    loadButton.disabled = true;
    loadButton.textContent = "Loading...";
    status.textContent = "Loading LOS facts...";

    try {
        validateInputs();
        const params = new URLSearchParams({
            startDate: startDate.value,
            endDate: endDate.value,
            lyComparisonBasis: lyComparisonBasis.value
        });
        const payload = await LosApi.fetchJson(`${API_BASE_URL}/los/facts?${params}`);
        loadedFacts = payload.data || [];
        render();
        status.textContent = loadedFacts.length ? "Data loaded." : "No data returned.";
    }
    catch (error) {
        console.error(error);
        errorPanel.hidden = false;
        errorPanel.textContent = error.message || "Unable to load distribution.";
        status.textContent = "Request failed.";
    }
    finally {
        loadButton.disabled = false;
        loadButton.textContent = "Load data";
    }
}

function render() {
    const hotel = hotelName.value.trim();
    const rows = LosData.calculateDistribution(loadedFacts, {
        grain: grain.value,
        hotelCodes: hotel ? [hotel] : null,
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

loadButton.addEventListener("click", loadData);
grain.addEventListener("change", render);
hotelName.addEventListener("input", render);
metric.addEventListener("change", render);
scenario.addEventListener("change", render);
level.addEventListener("change", render);
document.addEventListener("DOMContentLoaded", loadData);
