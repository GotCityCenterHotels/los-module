from shared.mews_source import agency_contains_text


DATE_PREDICATE = "stay_date BETWEEN %(start_date)s AND %(end_date)s"

# Dates and numerics are rendered to text HERE, not in Python.
#
# services/cost_data_service.py:_json_value used to convert every cell of every
# row: date.isoformat() at ~3us a call, str() on every Decimal, across seven
# datasets of a few thousand rows each. Worse, psycopg had to build the date and
# Decimal objects first, only for them to be thrown away a moment later as
# strings. PostgreSQL already holds the text form and can hand it over directly.
#
# The output is byte-identical to what Python produced: a date renders ISO as
# YYYY-MM-DD, and numeric::text is the same digit string str(Decimal) gives,
# scale included. Integer sums are cast ::bigint and arrive as Python ints, which
# need no conversion either.
#
# last_updated_at is deliberately NOT cast. It is timestamptz, and ::text renders
# it space-separated with a two-digit offset ("2026-01-03 08:00:00+00") rather
# than as the ISO 8601 the browser needs - frontend/costdata.js:1161 feeds it
# straight to new Date(), which rejects that spelling outright in Safari. One
# timestamp per row is a small share of the cells; a silently unparseable date is
# not worth the remainder.

# The one rule for "does this agency name contain this term". The two interactive
# pickers in services/cost_source_service.py apply the same one, so a term that
# found an agency in the editor is the term that charges it here.
AGENCY_MATCHES_TERM = agency_contains_text(
    "combination.travel_agency", "term.contains_value"
)


COST_DATA_QUERIES = {
    "arrivalsDepartures": f"""
        SELECT
            hotel.hotel_name,
            stay_date::text AS stay_date,
            sum(total_arrivals)::bigint AS total_arrivals,
            sum(total_departures)::bigint AS total_departures,
            max(fact.last_updated_at) AS last_updated_at
        FROM functions.arr_dep_data fact
        JOIN functions.hotels hotel USING (enterprise_id)
        WHERE {DATE_PREDICATE}
        GROUP BY hotel.hotel_name, stay_date
        ORDER BY stay_date, hotel.hotel_name
    """,
    "breakfast": f"""
        SELECT
            hotel.hotel_name,
            stay_date::text AS stay_date,
            sum(breakfast_total)::bigint AS breakfast_total,
            sum(breakfast_net_cost)::text AS breakfast_net_cost,
            max(fact.last_updated_at) AS last_updated_at
        FROM functions.breakfast_data fact
        JOIN functions.hotels hotel USING (enterprise_id)
        WHERE {DATE_PREDICATE}
        GROUP BY hotel.hotel_name, stay_date
        ORDER BY stay_date, hotel.hotel_name
    """,
    "parking": f"""
        SELECT
            hotel.hotel_name,
            stay_date::text AS stay_date,
            coalesce(nullif(trim(service), ''), 'Unspecified') AS service,
            sum(total_reservations_using_parking)::bigint
                AS total_reservations_using_parking,
            sum(total_parking_spots)::bigint AS total_parking_spots,
            sum(total_parking_amount_net_value)::text AS total_parking_amount_net_value,
            max(fact.last_updated_at) AS last_updated_at
        FROM functions.parking_data fact
        JOIN functions.hotels hotel USING (enterprise_id)
        WHERE {DATE_PREDICATE}
        GROUP BY hotel.hotel_name, stay_date, coalesce(nullif(trim(service), ''), 'Unspecified')
        ORDER BY stay_date, hotel.hotel_name, service
    """,
    "roomRevenue": f"""
        SELECT
            hotel.hotel_name,
            stay_date::text AS stay_date,
            coalesce(nullif(trim(amount_currency), ''), 'Unspecified') AS amount_currency,
            sum(room_revenue_excl_products_1_net)::text AS room_revenue_excl_products_1_net,
            sum(product_revenue_1_net)::text AS product_revenue_1_net,
            sum(room_revenue_incl_products_1_net)::text AS room_revenue_incl_products_1_net,
            max(fact.last_updated_at) AS last_updated_at
        FROM functions.room_revenue_night_data fact
        JOIN functions.hotels hotel USING (enterprise_id)
        WHERE {DATE_PREDICATE}
        GROUP BY hotel.hotel_name, stay_date,
            coalesce(nullif(trim(amount_currency), ''), 'Unspecified')
        ORDER BY stay_date, hotel.hotel_name, amount_currency
    """,
    "payments": f"""
        SELECT
            hotel.hotel_name,
            stay_date::text AS stay_date,
            coalesce(nullif(trim(amount_currency), ''), 'Unspecified') AS amount_currency,
            sum(total_payment_amount_gross_value)::text AS total_payment_amount_gross_value,
            max(fact.last_updated_at) AS last_updated_at
        FROM functions.total_payment_data fact
        JOIN functions.hotels hotel USING (enterprise_id)
        WHERE {DATE_PREDICATE}
        GROUP BY hotel.hotel_name, stay_date,
            coalesce(nullif(trim(amount_currency), ''), 'Unspecified')
        ORDER BY stay_date, hotel.hotel_name, amount_currency
    """,
    # One reservation cleaning allocated evenly across all of its occupied
    # nights.  The date is deliberately retained: dropping it was what made the
    # chart charge a multi-night stay entirely to its departure bucket.
    "cleaningAllocations": f"""
        SELECT
            hotel.hotel_name,
            fact.stay_date::text AS stay_date,
            fact.category_name,
            fact.occupancy,
            sum(fact.allocated_cleanings)::text AS allocated_cleanings,
            max(fact.last_updated_at) AS last_updated_at
        FROM functions.departure_mix_data fact
        JOIN functions.hotels hotel USING (enterprise_id)
        WHERE {DATE_PREDICATE}
          AND fact.allocated_cleanings IS NOT NULL
        GROUP BY hotel.hotel_name, fact.stay_date,
                 fact.category_name, fact.occupancy
        ORDER BY fact.stay_date, hotel.hotel_name,
                 fact.category_name, fact.occupancy
    """,
    # The revenue-weighted distribution percentage for each hotel and day.
    #
    # The rulebook and the mix live in the same database, so the matching is done
    # here rather than in the browser: a year of reservation-level mix rows is two
    # orders of magnitude more than the page's other datasets put together, and
    # shipping them to the client to be matched there would have been the single
    # largest payload on the page. What travels is one row per hotel per day.
    #
    # Only the tree is applied here. Revenue that matches no origin group comes
    # back as the difference between mix_revenue and matched_revenue, and the
    # page charges that share at the property's fallback percentage - so the
    # fallback stays defined in exactly one place.
    "distributionRates": f"""
        WITH mix AS (
            SELECT
                fact.enterprise_id,
                hotel.hotel_name,
                fact.stay_date,
                fact.origin,
                fact.travel_agency,
                fact.rate_name,
                sum(fact.room_revenue_net) AS revenue,
                max(fact.last_updated_at) AS last_updated_at
            FROM functions.distribution_mix_data fact
            JOIN functions.hotels hotel USING (enterprise_id)
            WHERE {DATE_PREDICATE}
            GROUP BY fact.enterprise_id, hotel.hotel_name, fact.stay_date,
                     fact.origin, fact.travel_agency, fact.rate_name
        ),
        -- Priced per distinct combination rather than per hotel-day-combination:
        -- the percentage does not vary by date, and a year of daily rows would
        -- otherwise run the lateral lookup tens of thousands of times for the
        -- same handful of answers.
        combinations AS (
            SELECT DISTINCT enterprise_id, origin, travel_agency, rate_name
            FROM mix
        ),
        -- MATERIALIZED is load-bearing. Without it PostgreSQL inlines this CTE
        -- under the final daily-row join and executes the lateral rule lookup
        -- once per hotel/day/combination row. A production-year plan priced
        -- 28,848 rows instead of the 989 distinct combinations above.
        priced AS MATERIALIZED (
            SELECT
                combination.enterprise_id,
                combination.origin,
                combination.travel_agency,
                combination.rate_name,
                matched.cost_percent
            FROM combinations combination
            LEFT JOIN LATERAL (
                -- The most specific level that matches wins, which is what the
                -- editor promises: a rate group's percentage over its subgroup's
                -- fallback, over its origin group's fallback.
                SELECT coalesce(
                    rate_group.cost_percent,
                    agency_group.fallback_percent,
                    origin_group.fallback_percent
                ) AS cost_percent
                FROM functions.cost_distribution_origin_groups origin_group
                JOIN functions.cost_distribution_origin_values group_origin
                  ON group_origin.origin_group_id = origin_group.origin_group_id
                 AND lower(btrim(group_origin.origin_value))
                     = lower(btrim(combination.origin))
                LEFT JOIN functions.cost_distribution_agency_groups agency_group
                  ON agency_group.origin_group_id = origin_group.origin_group_id
                 AND (
                     -- A subgroup with no search term is agency-agnostic: it
                     -- covers the whole origin group and its rate groups are
                     -- what discriminate. That is the reading the editor's own
                     -- warning text describes.
                     NOT EXISTS (
                         SELECT 1
                         FROM functions.cost_distribution_agency_filters term
                         WHERE term.agency_group_id = agency_group.agency_group_id
                           AND nullif(btrim(term.contains_value), '') IS NOT NULL
                     )
                     -- Any one term is enough: a subgroup's terms are a union, so
                     -- an agency that spells itself two ways gets a row each.
                     OR EXISTS (
                         SELECT 1
                         FROM functions.cost_distribution_agency_filters term
                         WHERE term.agency_group_id = agency_group.agency_group_id
                           AND nullif(btrim(term.contains_value), '') IS NOT NULL
                           -- Folded on both sides, so "booking.com" charges
                           -- "Booking.com B.V." and "BOOKING COM" alike. strpos
                           -- rather than ILIKE also means a per cent sign or an
                           -- underscore in an agency name stays a character
                           -- rather than becoming a wildcard.
                           AND {AGENCY_MATCHES_TERM}
                     )
                 )
                LEFT JOIN functions.cost_distribution_rate_groups rate_group
                  ON rate_group.agency_group_id = agency_group.agency_group_id
                 AND EXISTS (
                     SELECT 1
                     FROM functions.cost_distribution_rate_values rate_value
                     WHERE rate_value.rate_group_id = rate_group.rate_group_id
                       AND lower(btrim(rate_value.rate_name))
                           = lower(btrim(combination.rate_name))
                 )
                WHERE origin_group.enterprise_id = combination.enterprise_id
                ORDER BY
                    (rate_group.rate_group_id IS NOT NULL) DESC,
                    (agency_group.agency_group_id IS NOT NULL) DESC,
                    agency_group.sort_order,
                    rate_group.sort_order
                LIMIT 1
            ) matched ON true
        )
        SELECT
            mix.hotel_name,
            mix.stay_date::text AS stay_date,
            sum(mix.revenue)::text AS mix_revenue,
            coalesce(
                sum(mix.revenue) FILTER (WHERE priced.cost_percent IS NOT NULL), 0
            )::text AS matched_revenue,
            (
                sum(mix.revenue * priced.cost_percent)
                    FILTER (WHERE priced.cost_percent IS NOT NULL)
                / nullif(
                    sum(mix.revenue) FILTER (WHERE priced.cost_percent IS NOT NULL), 0
                )
            )::text AS matched_percent,
            max(mix.last_updated_at) AS last_updated_at
        FROM mix
        JOIN priced
          ON priced.enterprise_id = mix.enterprise_id
         -- IS NOT DISTINCT FROM has the right null semantics here but cannot
         -- drive a hash join. Pairing coalesce equality with an explicit null
         -- flag is exactly equivalent (including null versus empty text) and
         -- lets PostgreSQL hash the 989 priced combinations against the daily
         -- rows instead of testing millions of pairs.
         AND coalesce(priced.origin, '') = coalesce(mix.origin, '')
         AND (priced.origin IS NULL) = (mix.origin IS NULL)
         AND coalesce(priced.travel_agency, '') = coalesce(mix.travel_agency, '')
         AND (priced.travel_agency IS NULL) = (mix.travel_agency IS NULL)
         AND coalesce(priced.rate_name, '') = coalesce(mix.rate_name, '')
         AND (priced.rate_name IS NULL) = (mix.rate_name IS NULL)
        GROUP BY mix.hotel_name, mix.stay_date
        ORDER BY mix.stay_date, mix.hotel_name
    """,
}
