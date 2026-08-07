HOTELS_SQL = """
SELECT DISTINCT
    trim(hotel_name)::text AS hotel_name
FROM staging.room_nights_source
WHERE
    hotel_name IS NOT NULL
    AND trim(hotel_name) <> ''
ORDER BY hotel_name;
"""
