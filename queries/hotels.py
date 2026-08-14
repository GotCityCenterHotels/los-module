HOTELS_SQL = """
WITH hotels AS (
    /* Keep both date predicates directly beside the source relation. */
    SELECT enterprise.id::text AS hotel_code,
           trim(enterprise.name)::text AS hotel_name
    FROM staging.room_nights_source r
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = 'GCCH'
     AND trim(enterprise.name) = trim(r.hotel_name)
    WHERE
        r.hotel_name IS NOT NULL
        AND trim(r.hotel_name) <> ''
        AND r.start_utc >= (
            %(start_date)s::date::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            (%(end_date)s::date + 1)::timestamp
            AT TIME ZONE 'Europe/Stockholm'
        )

    UNION ALL

    SELECT enterprise.id::text AS hotel_code,
           trim(enterprise.name)::text AS hotel_name
    FROM staging.room_nights_source r
    JOIN enterprise_current enterprise
      ON enterprise.tenant_key = 'GCCH'
     AND trim(enterprise.name) = trim(r.hotel_name)
    WHERE
        r.hotel_name IS NOT NULL
        AND trim(r.hotel_name) <> ''
        AND r.start_utc >= (
            (
                CASE
                    WHEN %(ly_comparison_basis)s = 'sameWeekday'
                        THEN %(start_date)s::date - 364
                    ELSE (%(start_date)s::date - INTERVAL '1 year')::date
                END
            )::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
        AND r.start_utc < (
            (
                CASE
                    WHEN %(ly_comparison_basis)s = 'sameWeekday'
                        THEN %(end_date)s::date - 364 + 1
                    ELSE ((%(end_date)s::date - INTERVAL '1 year')::date + 1)
                END
            )::timestamp AT TIME ZONE 'Europe/Stockholm'
        )
)
SELECT DISTINCT hotel_code, hotel_name
FROM hotels
ORDER BY hotel_name, hotel_code;
"""
