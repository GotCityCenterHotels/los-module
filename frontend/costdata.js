(function initializeCostDataView() {
    "use strict";

    const API_URL = "/api/costdata/facts";
    const DATASETS = {
        roomRevenue: {
            title: "Room revenue",
            columns: [
                ["stayDate", "Period", "date"],
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
                ["stayDate", "Period", "date"],
                ["hotelName", "Hotel", "text"],
                ["amountCurrency", "Currency", "text"],
                ["totalPaymentAmountGrossValue", "Total payment gross", "decimal"],
                ["lastUpdatedAt", "Updated", "datetime"]
            ]
        },
        breakfast: {
            title: "Breakfast",
            columns: [
                ["stayDate", "Period", "date"],
                ["hotelName", "Hotel", "text"],
                ["breakfastTotal", "Breakfasts", "integer"],
                ["breakfastNetCost", "Net cost", "decimal"],
                ["lastUpdatedAt", "Updated", "datetime"]
            ]
        },
        parking: {
            title: "Parking",
            columns: [
                ["stayDate", "Period", "date"],
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
                ["stayDate", "Period", "date"],
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
        summary: document.getElementById("costSummary"),
        results: document.getElementById("costResults"),
        error: document.getElementById("costError"),
        title: document.getElementById("costTableTitle"),
        rowCount: document.getElementById("costRowCount"),
        head: document.getElementById("costTableHead"),
        body: document.getElementById("costTableBody"),
        freshness: document.getElementById("freshness"),
        latestUpdate: document.getElementById("latestUpdate"),
        revenue: document.getElementById("summaryRevenue"),
        payments: document.getElementById("summaryPayments"),
        breakfast: document.getElementById("summaryBreakfast"),
        parking: document.getElementById("summaryParking"),
        movement: document.getElementById("summaryMovement")
    };

    const numberFormatter = new Intl.NumberFormat("en-SE", { maximumFractionDigits: 2 });
    const integerFormatter = new Intl.NumberFormat("en-SE", { maximumFractionDigits: 0 });
    const dateFormatter = new Intl.DateTimeFormat("en-SE", {
        year: "numeric", month: "short", day: "numeric"
    });
    const dateTimeFormatter = new Intl.DateTimeFormat("en-SE", {
        year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
    });

    let loadedData = null;
    let activeDataset = "roomRevenue";

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
            elements.status.textContent = "Reading the functions schema…";
            const parameters = new URLSearchParams({
                startDate: elements.startDate.value,
                endDate: elements.endDate.value
            });
            const payload = await LosApi.fetchJson(`${API_URL}?${parameters}`);
            loadedData = payload.data || {};
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
        renderSummary();
        renderTable();
        elements.scope.textContent = [
            elements.hotel.value || "All hotels",
            `${elements.startDate.value} – ${elements.endDate.value}`
        ].join(" · ");
        elements.summary.hidden = false;
        elements.results.hidden = false;
    }

    function renderSummary() {
        const summary = CostData.summarize(loadedData, { hotelName: elements.hotel.value });
        elements.revenue.replaceChildren(...currencySummaryNodes(summary.roomRevenue));
        elements.payments.replaceChildren(...currencySummaryNodes(summary.payments));
        elements.breakfast.textContent = formatDecimal(summary.breakfastCost);
        elements.parking.textContent = formatDecimal(summary.parkingNet);
        elements.movement.textContent = `${integerFormatter.format(summary.arrivals)} in · ${integerFormatter.format(summary.departures)} out`;
    }

    function currencySummaryNodes(totals) {
        const entries = Object.entries(totals);
        if (!entries.length) return [document.createTextNode("—")];
        const fragmentNodes = [];
        entries.sort(([left], [right]) => left.localeCompare(right)).forEach(([currency, total], index) => {
            if (index) fragmentNodes.push(document.createElement("br"));
            const line = document.createElement("span");
            line.textContent = `${formatDecimal(total)} ${currency === "Unspecified" ? "" : currency}`.trim();
            fragmentNodes.push(line);
        });
        return fragmentNodes;
    }

    function renderTable() {
        const definition = DATASETS[activeDataset];
        const rows = CostData.aggregate(activeDataset, loadedData[activeDataset] || [], {
            grain: elements.grain.value,
            hotelName: elements.hotel.value
        });
        elements.title.textContent = definition.title;
        elements.rowCount.textContent = `${integerFormatter.format(rows.length)} ${rows.length === 1 ? "row" : "rows"}`;
        elements.head.replaceChildren(buildHeader(definition.columns));
        elements.body.replaceChildren(...buildRows(rows, definition.columns));
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

    function buildRows(rows, columns) {
        if (!rows.length) {
            const row = document.createElement("tr");
            const cell = document.createElement("td");
            cell.colSpan = columns.length;
            cell.className = "cost-empty";
            cell.textContent = "No rows in this dataset for the selected scope.";
            row.appendChild(cell);
            return [row];
        }

        return rows.map((item) => {
            const row = document.createElement("tr");
            for (const [field, , type] of columns) {
                const cell = document.createElement("td");
                cell.textContent = formatCell(item[field], type);
                if (["integer", "decimal"].includes(type)) cell.className = "numeric-column";
                row.appendChild(cell);
            }
            return row;
        });
    }

    function formatCell(value, type) {
        if (value === null || value === undefined || value === "") return "—";
        if (type === "integer") return integerFormatter.format(Number(value));
        if (type === "decimal") return formatDecimal(value);
        if (type === "datetime") return formatDateTime(value);
        if (type === "date") return dateFormatter.format(new Date(`${value}T00:00:00`));
        return String(value);
    }

    function formatDecimal(value) {
        return Number.isFinite(Number(value)) ? numberFormatter.format(Number(value)) : "—";
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
    elements.grain.addEventListener("change", renderTable);
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
}());
