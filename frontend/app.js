/*
=====================================================
API CONFIGURATION
=====================================================

Preferred:
    "/api"

This works when your existing Function App is linked
to Azure Static Web Apps.

If you DON'T link it, replace API_BASE_URL with:

"https://los-functions-dkeaapbyb6f8ebd7.swedencentral-01.azurewebsites.net/api"

=====================================================
*/

const API_BASE_URL = "/api";


const startDateInput =
    document.getElementById("startDate");

const endDateInput =
    document.getElementById("endDate");

const grainInput =
    document.getElementById("grain");

const hotelNameInput =
    document.getElementById("hotelName");

const lyComparisonInput =
    document.getElementById(
        "lyComparisonBasis"
    );


const loadButton =
    document.getElementById("loadButton");


const statusElement =
    document.getElementById("status");


const summaryElement =
    document.getElementById("summary");

const rowCountElement =
    document.getElementById("rowCount");

const selectedStartElement =
    document.getElementById("selectedStart");

const selectedEndElement =
    document.getElementById("selectedEnd");

const selectedGrainElement =
    document.getElementById("selectedGrain");


const resultsSection =
    document.getElementById(
        "resultsSection"
    );

const resultsBody =
    document.getElementById(
        "resultsBody"
    );


const errorPanel =
    document.getElementById(
        "errorPanel"
    );


/* =====================================================
   NUMBER FORMATTERS
   ===================================================== */

const decimalFormatter =
    new Intl.NumberFormat(
        "en-SE",
        {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        }
    );


const integerFormatter =
    new Intl.NumberFormat(
        "en-SE",
        {
            maximumFractionDigits: 0
        }
    );


function formatDecimal(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "-";
    }

    return decimalFormatter.format(
        Number(value)
    );
}


function formatInteger(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "-";
    }

    return integerFormatter.format(
        Number(value)
    );
}


/* =====================================================
   BUILD REQUEST URL
   ===================================================== */

function buildRequestUrl() {

    const params =
        new URLSearchParams();


    params.set(
        "startDate",
        startDateInput.value
    );


    params.set(
        "endDate",
        endDateInput.value
    );


    params.set(
        "grain",
        grainInput.value
    );


    params.set(
        "lyComparisonBasis",
        lyComparisonInput.value
    );


    const hotelName =
        hotelNameInput.value.trim();


    if (hotelName) {

        params.set(
            "hotelName",
            hotelName
        );

    }


    return (
        `${API_BASE_URL}/los/average?`
        + params.toString()
    );
}


/* =====================================================
   VALIDATE
   ===================================================== */

function validateInputs() {

    if (!startDateInput.value) {

        throw new Error(
            "Start date is required."
        );

    }


    if (!endDateInput.value) {

        throw new Error(
            "End date is required."
        );

    }


    if (
        startDateInput.value
        >
        endDateInput.value
    ) {

        throw new Error(
            "Start date cannot be after end date."
        );

    }

}


/* =====================================================
   LOAD DATA
   ===================================================== */

async function loadData() {

    clearError();


    try {

        validateInputs();


        loadButton.disabled = true;

        loadButton.textContent =
            "Loading...";


        statusElement.textContent =
            "Loading LOS data...";


        const requestUrl =
            buildRequestUrl();


        console.log(
            "Request:",
            requestUrl
        );


        const response =
            await fetch(requestUrl);


        let result;


        try {

            result =
                await response.json();

        }
        catch {

            throw new Error(
                `API returned HTTP `
                + `${response.status} `
                + `but did not return JSON.`
            );

        }


        if (!response.ok) {

            throw new Error(
                result.error
                ||
                `API request failed `
                + `with HTTP `
                + response.status
            );

        }


        renderSummary(result);

        renderTable(
            result.data || []
        );


        statusElement.textContent =
            `Loaded ${result.rowCount} rows.`;


    }
    catch (error) {

        console.error(error);

        showError(
            error.message
            ||
            "Unable to load data."
        );


        statusElement.textContent =
            "Request failed.";

    }
    finally {

        loadButton.disabled = false;

        loadButton.textContent =
            "Load data";

    }

}


/* =====================================================
   SUMMARY
   ===================================================== */

function renderSummary(result) {

    summaryElement.hidden = false;


    rowCountElement.textContent =
        result.rowCount ?? 0;


    selectedStartElement.textContent =
        result.parameters?.startDate
        ?? "-";


    selectedEndElement.textContent =
        result.parameters?.endDate
        ?? "-";


    selectedGrainElement.textContent =
        result.parameters?.grain
        ?? "-";

}


/* =====================================================
   TABLE
   ===================================================== */

function renderTable(rows) {

    resultsBody.innerHTML = "";


    if (rows.length === 0) {

        resultsSection.hidden = false;


        const row =
            document.createElement("tr");


        row.innerHTML = `
            <td
                colspan="11"
                style="text-align:center"
            >
                No data returned.
            </td>
        `;


        resultsBody.appendChild(row);

        return;
    }


    for (const item of rows) {

        const row =
            document.createElement("tr");


        if (
            item.hotel_code === "Total"
        ) {

            row.classList.add(
                "total-row"
            );

        }


        row.innerHTML = `
            <td>
                ${escapeHtml(
                    item.bucket_date
                )}
            </td>

            <td>
                ${escapeHtml(
                    item.hotel_code
                )}
            </td>

            <td>
                ${formatDecimal(
                    item.los
                )}
            </td>

            <td>
                ${formatDecimal(
                    item.losly
                )}
            </td>

            <td>
                ${formatDecimal(
                    item.spit_los_non_strict_arrival
                )}
            </td>

            <td>
                ${formatInteger(
                    item.rn
                )}
            </td>

            <td>
                ${formatInteger(
                    item.rnly
                )}
            </td>

            <td>
                ${formatInteger(
                    item.spit_rn_non_strict_arrival
                )}
            </td>

            <td>
                ${formatInteger(
                    item.total_bookings
                )}
            </td>

            <td>
                ${formatInteger(
                    item.total_bookings_ly
                )}
            </td>

            <td>
                ${formatInteger(
                    item.total_bookings_spit
                )}
            </td>
        `;


        resultsBody.appendChild(row);

    }


    resultsSection.hidden = false;

}


/* =====================================================
   BASIC HTML ESCAPING
   ===================================================== */

function escapeHtml(value) {

    if (
        value === null ||
        value === undefined
    ) {
        return "";
    }


    return String(value)

        .replaceAll(
            "&",
            "&amp;"
        )

        .replaceAll(
            "<",
            "&lt;"
        )

        .replaceAll(
            ">",
            "&gt;"
        )

        .replaceAll(
            '"',
            "&quot;"
        )

        .replaceAll(
            "'",
            "&#039;"
        );

}


/* =====================================================
   ERRORS
   ===================================================== */

function showError(message) {

    errorPanel.hidden = false;

    errorPanel.textContent =
        message;

}


function clearError() {

    errorPanel.hidden = true;

    errorPanel.textContent = "";

}


/* =====================================================
   EVENT LISTENERS
   ===================================================== */

loadButton.addEventListener(
    "click",
    loadData
);


/*
Optional:
Load automatically when page opens.
*/

document.addEventListener(
    "DOMContentLoaded",
    loadData
);