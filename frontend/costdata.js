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
        results: document.getElementById("costResults"),
        error: document.getElementById("costError"),
        title: document.getElementById("costTableTitle"),
        rowCount: document.getElementById("costRowCount"),
        head: document.getElementById("costTableHead"),
        body: document.getElementById("costTableBody"),
        freshness: document.getElementById("freshness"),
        latestUpdate: document.getElementById("latestUpdate")
    };

    const integerFormatter = new Intl.NumberFormat("en-SE", { maximumFractionDigits: 0 });
    const dateTimeFormatter = new Intl.DateTimeFormat("en-SE", {
        year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
    });

    let loadedData = null;
    let loadedSettings = {};
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
        elements.results.hidden = false;
    }

    // A cost is money out; showing it as a bare positive number next to revenue
    // makes the statement impossible to read down the column. A correction
    // period can produce a negative cost, which is a credit and reads as one.
    function signedCost(amount) {
        return amount < 0
            ? `+${LosFormat.formatSek(-amount)}`
            : `−${LosFormat.formatSek(amount)}`;
    }

    // The GOP statement is net of VAT throughout: every figure is a net revenue
    // stream or a cost derived from Cost Input. No gross figure appears here.
    function renderGop() {
        const statement = CostData.calculateGop(loadedData, {
            hotelName: elements.hotel.value,
            settingsByHotel: loadedSettings
        });

        elements.gopScope.textContent = statement.hotels.length
            ? `${statement.currency} · net excl. VAT · ${statement.hotels.length} `
                + `${statement.hotels.length === 1 ? "property" : "properties"}`
            : "No properties in this scope";

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
            cell.textContent = "No rows in this dataset for the selected scope.";
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
        if (type === "integer") return integerFormatter.format(Number(value));
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
