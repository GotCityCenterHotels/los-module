/*
LOS definition parity check -- run manually in pgAdmin.

Edit the two dates below to select a representative arrival population. The
script does not choose which LOS definition is authoritative; it only reports
reservation-level differences. The production query remains room-night based
until these results are reviewed.

This uses staging.room_nights_source because that is the source relation used
by the current application. It assumes the view exposes reservation end_utc,
as described by the reservation_current source model. If your deployed view
does not expose end_utc, add that column to the diagnostic view or join its
underlying reservation_current relation using the reservation key before
running this script. Do not substitute a guessed join key.
*/

DROP TABLE IF EXISTS pg_temp.los_definition_parity;

CREATE TEMP TABLE los_definition_parity AS
WITH reservation_comparison AS (
    SELECT
        r.number::text AS reservation_id,
        trim(r.hotel_name)::text AS hotel_code,
        (r.start_utc AT TIME ZONE 'Europe/Stockholm')::date AS arrival_date,
        (max(r.end_utc) AT TIME ZONE 'Europe/Stockholm')::date AS departure_date,
        count(DISTINCT (
            r.night_start_utc AT TIME ZONE 'Europe/Stockholm'
        )::date)::int AS los_from_room_nights
    FROM staging.room_nights_source r
    WHERE
        r.number IS NOT NULL
        AND r.start_utc IS NOT NULL
        AND r.canceled_utc IS NULL
        AND r.start_utc >= (
            DATE '2026-01-01'::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            DATE '2027-01-01'::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
    GROUP BY
        r.number::text,
        trim(r.hotel_name)::text,
        (r.start_utc AT TIME ZONE 'Europe/Stockholm')::date
)
SELECT
    reservation_id,
    hotel_code,
    arrival_date,
    departure_date,
    los_from_room_nights,
    CASE
        WHEN departure_date IS NULL THEN NULL
        ELSE departure_date - arrival_date
    END::int AS los_from_dates,
    CASE
        WHEN departure_date IS NULL THEN NULL
        ELSE los_from_room_nights - (departure_date - arrival_date)
    END::int AS difference
FROM reservation_comparison;

/* Detail: mismatches first, followed by matching rows for spot checks. */
SELECT
    reservation_id,
    hotel_code,
    arrival_date,
    departure_date,
    los_from_room_nights,
    los_from_dates,
    difference
FROM los_definition_parity
ORDER BY
    (difference IS DISTINCT FROM 0) DESC,
    abs(difference) DESC NULLS LAST,
    arrival_date,
    hotel_code,
    reservation_id;

/* Required summary. Missing room nights means the distinct-night count is 0. */
SELECT
    count(*)::bigint AS total_reservations,
    count(*) FILTER (WHERE difference = 0)::bigint AS matching_reservations,
    count(*) FILTER (WHERE difference IS DISTINCT FROM 0)::bigint
        AS mismatching_reservations,
    round(
        100.0 * count(*) FILTER (WHERE difference = 0)
        / nullif(count(*), 0),
        4
    ) AS match_percentage,
    count(*) FILTER (WHERE los_from_room_nights = 0)::bigint AS missing_room_nights,
    count(*) FILTER (WHERE departure_date IS NULL)::bigint AS missing_departure_date,
    count(*) FILTER (WHERE los_from_dates <= 0)::bigint AS zero_or_negative_date_los
FROM los_definition_parity;

/* Compact mismatch patterns help identify whether discrepancies are systematic. */
SELECT
    los_from_room_nights,
    los_from_dates,
    difference,
    count(*)::bigint AS reservation_count
FROM los_definition_parity
WHERE difference IS DISTINCT FROM 0
GROUP BY
    los_from_room_nights,
    los_from_dates,
    difference
ORDER BY reservation_count DESC, difference NULLS LAST;
