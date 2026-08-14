SELECT
    md5(
        concat_ws(
            '|',
            ec.id::text,
            e.stay_date::text
        )
    ) AS arr_dep_data_key,

    ec.id::text AS enterprise_id,
    trim(ec.name)::text AS hotel_name,
    e.stay_date,

    count(distinct r.reservation_id) FILTER (
        WHERE e.event_type = 'arrival'
    ) AS total_arrivals,

    count(distinct r.reservation_id) FILTER (
        WHERE e.event_type = 'departure'
    ) AS total_departures

FROM staging.room_nights_source r

JOIN enterprise_current ec
  ON ec.tenant_key = 'GCCH'
 AND trim(ec.name) = trim(r.hotel_name)

CROSS JOIN LATERAL (
    VALUES
        ((r.start_utc AT TIME ZONE 'Europe/Stockholm')::date, 'arrival'),
        ((r.end_utc   AT TIME ZONE 'Europe/Stockholm')::date, 'departure')
) e(stay_date, event_type)

WHERE r.canceled_utc IS NULL
  AND e.stay_date IS NOT NULL

GROUP BY
    ec.id,
    ec.name,
    e.stay_date

ORDER BY
    ec.name,
    e.stay_date;
