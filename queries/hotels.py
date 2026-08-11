HOTELS_SQL = """
WITH hotel_codes AS (
    /* Keep both date predicates directly beside the source relation. */
    SELECT trim(r.hotel_name)::text AS hotel_code
    FROM staging.room_nights_source r
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

    SELECT trim(r.hotel_name)::text AS hotel_code
    FROM staging.room_nights_source r
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
SELECT DISTINCT hotel_code
FROM hotel_codes
ORDER BY hotel_code;
"""
