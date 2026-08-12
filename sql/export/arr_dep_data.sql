SELECT
    md5(
        concat_ws(
            '|',
            r.hotel_name,
            e.stay_date::text
        )
    ) AS arr_dep_data_key,

    r.hotel_name,
    e.stay_date,

    count(distinct r.reservation_id) FILTER (
        WHERE e.event_type = 'arrival'
    ) AS total_arrivals,

    count(distinct r.reservation_id) FILTER (
        WHERE e.event_type = 'departure'
    ) AS total_departures

FROM staging.room_nights_source r

CROSS JOIN LATERAL (
    VALUES
        ((r.start_utc AT TIME ZONE 'Europe/Stockholm')::date, 'arrival'),
        ((r.end_utc   AT TIME ZONE 'Europe/Stockholm')::date, 'departure')
) e(stay_date, event_type)

WHERE r.canceled_utc IS NULL
  AND e.stay_date IS NOT NULL

GROUP BY
    r.hotel_name,
    e.stay_date

ORDER BY
    r.hotel_name,
    e.stay_date;