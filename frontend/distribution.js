const API_BASE_URL = "/api";


const startDate =
    document.getElementById("startDate");

const endDate =
    document.getElementById("endDate");

const grain =
    document.getElementById("grain");

const hotelName =
    document.getElementById("hotelName");

const lyComparisonBasis =
    document.getElementById(
        "lyComparisonBasis"
    );

const metric =
    document.getElementById("metric");

const scenario =
    document.getElementById("scenario");

const level =
    document.getElementById("level");

const loadButton =
    document.getElementById("loadButton");

const status =
    document.getElementById("status");

const results =
    document.getElementById(
        "distributionResults"
    );

const errorPanel =
    document.getElementById("errorPanel");


let loadedData = [];


/* ============================================================
   FETCH DATA
   ============================================================ */

async function loadData() {

    errorPanel.hidden = true;

    loadButton.disabled = true;

    status.textContent =
        "Loading distribution...";


    try {

        const params =
            new URLSearchParams();


        params.set(
            "startDate",
            startDate.value
        );

        params.set(
            "endDate",
            endDate.value
        );

        params.set(
            "grain",
            grain.value
        );

        params.set(
            "lyComparisonBasis",
            lyComparisonBasis.value
        );


        const hotel =
            hotelName.value.trim();


        if (hotel) {

            params.set(
                "hotelName",
                hotel
            );

        }


        const response =
            await fetch(
                `${API_BASE_URL}/los/distribution?${params}`
            );


        const payload =
            await response.json();


        if (!response.ok) {

            throw new Error(
                payload.error ||
                `HTTP ${response.status}`
            );

        }


        loadedData =
            payload.data || [];


        render();


        status.textContent = loadedData.length
            ? "Data loaded."
            : "No data returned.";

    }

    catch (error) {

        console.error(error);

        errorPanel.hidden = false;

        errorPanel.textContent =
            error.message;

        status.textContent =
            "Request failed.";

    }

    finally {

        loadButton.disabled = false;

    }

}


/* ============================================================
   SELECT WHICH ROWS TO DISPLAY
   ============================================================ */

function getDisplayRows() {

    let rows = [...loadedData];


    if (
        scenario.value !== "all"
    ) {

        rows = rows.filter(
            row =>
                row.scenario ===
                scenario.value
        );

    }


    if (
        level.value === "total"
    ) {

        rows = rows.filter(
            row =>
                row.hotel_code ===
                "Total"
        );

    }

    else {

        rows = rows.filter(
            row =>
                row.hotel_code !==
                "Total"
        );

    }


    return rows;

}


/* ============================================================
   GET VALUES FOR BOOKING / NIGHT VIEW
   ============================================================ */

function getDistribution(row) {

    if (
        metric.value === "nights"
    ) {

        return {

            total:
                Number(
                    row.total_nights || 0
                ),

            values: [
                Number(
                    row.los_1_nights || 0
                ),

                Number(
                    row.los_2_nights || 0
                ),

                Number(
                    row.los_3_nights || 0
                ),

                Number(
                    row.los_4_nights || 0
                ),

                Number(
                    row.los_5_plus_nights || 0
                ),
            ]
        };

    }


    return {

        total:
            Number(
                row.total_bookings || 0
            ),

        values: [
            Number(
                row.los_1_bookings || 0
            ),

            Number(
                row.los_2_bookings || 0
            ),

            Number(
                row.los_3_bookings || 0
            ),

            Number(
                row.los_4_bookings || 0
            ),

            Number(
                row.los_5_plus_bookings || 0
            ),
        ]
    };

}


/* ============================================================
   RENDER
   ============================================================ */

function render() {

    const rows =
        getDisplayRows();


    results.innerHTML = "";


    if (
        rows.length === 0
    ) {

        results.innerHTML = `
            <div class="summary-card">
                No distribution data found.
            </div>
        `;

        return;

    }


    for (
        const row of rows
    ) {

        renderRow(row);

    }

}


/* ============================================================
   RENDER ONE DISTRIBUTION BAR
   ============================================================ */

function renderRow(row) {

    const distribution =
        getDistribution(row);


    const total =
        distribution.total;


    const percentages =
        distribution.values.map(
            value =>
                total > 0
                    ? value / total * 100
                    : 0
        );


    const card =
        document.createElement("div");


    card.className =
        "distribution-card";


    card.innerHTML = `

        <div class="distribution-heading">

            <div>
                <strong>
                    ${escapeHtml(row.bucket_date)}
                </strong>

                <span>
                    ${escapeHtml(row.hotel_code)}
                </span>
            </div>

            <div>
                ${scenarioLabel(row.scenario)}
                ·
                ${formatNumber(total)}
                ${metric.value === "bookings"
                    ? "reservations"
                    : "nights"}
            </div>

        </div>


        <div class="distribution-bar">

            ${segment(
                "los-1",
                percentages[0],
                "LOS 1"
            )}

            ${segment(
                "los-2",
                percentages[1],
                "LOS 2"
            )}

            ${segment(
                "los-3",
                percentages[2],
                "LOS 3"
            )}

            ${segment(
                "los-4",
                percentages[3],
                "LOS 4"
            )}

            ${segment(
                "los-5",
                percentages[4],
                "LOS 5+"
            )}

        </div>


        <div class="distribution-values">

            ${valueItem(
                "LOS 1",
                distribution.values[0],
                percentages[0]
            )}

            ${valueItem(
                "LOS 2",
                distribution.values[1],
                percentages[1]
            )}

            ${valueItem(
                "LOS 3",
                distribution.values[2],
                percentages[2]
            )}

            ${valueItem(
                "LOS 4",
                distribution.values[3],
                percentages[3]
            )}

            ${valueItem(
                "LOS 5+",
                distribution.values[4],
                percentages[4]
            )}

        </div>
    `;


    results.appendChild(card);

}


function segment(
    cssClass,
    percentage,
    label
) {

    return `
        <div
            class="distribution-segment ${cssClass}"
            style="width:${percentage}%"
            title="${label}: ${percentage.toFixed(1)}%"
        >
        </div>
    `;

}


function valueItem(
    label,
    value,
    percentage
) {

    return `
        <div class="distribution-value">

            <strong>
                ${label}
            </strong>

            <span>
                ${formatNumber(value)}
                ·
                ${percentage.toFixed(1)}%
            </span>

        </div>
    `;

}


function scenarioLabel(value) {

    switch (value) {

        case "ly":
            return "Actual LY";

        case "spit":
            return "SPIT";

        default:
            return "Current";

    }

}


function formatNumber(value) {

    return new Intl.NumberFormat(
        "en-SE"
    ).format(value);

}


function escapeHtml(value) {

    return String(
        value ?? ""
    )

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


/* ============================================================
   EVENTS

   Metric/scenario/display changes do NOT re-query PostgreSQL.
   They reuse the already downloaded result.
   ============================================================ */

loadButton.addEventListener(
    "click",
    loadData
);


metric.addEventListener(
    "change",
    render
);


scenario.addEventListener(
    "change",
    render
);


level.addEventListener(
    "change",
    render
);


document.addEventListener(
    "DOMContentLoaded",
    loadData
);
