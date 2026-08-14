-- Measure the impact of the SPIT booking-count fix. READ ONLY.
--
-- Run against Database A (the application database) BEFORE trusting the new
-- numbers. It reproduces the old and new SPIT definitions side by side from
-- functions.reservation_los_fact and reports where they disagree.
--
-- Old definition: count stored fact rows. A reservation shortened after the
--   cutoff is stored as several rows, so it was counted several times and its
--   length of stay was split across them.
-- New definition: collapse to one row per reservation (summing length of stay)
--   before counting.
--
-- Set the cutoff to match the sameDate basis for today.
\set cutoff '(CURRENT_DATE - INTERVAL ''1 year'')::date'

WITH alive AS (
    SELECT
        fact.reservation_number,
        fact.enterprise_id,
        fact.arrival_date,
        fact.los,
        fact.cancelled_date
    FROM functions.reservation_los_fact fact
    WHERE fact.fact_kind = 'historical'
      AND fact.created_date <= :cutoff
      AND (fact.cancelled_date IS NULL OR fact.cancelled_date > :cutoff)
),
per_reservation AS (
    SELECT
        reservation_number,
        enterprise_id,
        arrival_date,
        count(*) AS stored_rows,
        sum(los)::int AS true_los,
        min(los)::int AS smallest_split,
        max(los)::int AS largest_split
    FROM alive
    GROUP BY 1, 2, 3
)
SELECT
    (SELECT count(*) FROM per_reservation)                        AS reservations_in_spit,
    (SELECT count(*) FROM per_reservation WHERE stored_rows > 1)  AS reservations_miscounted,
    round(
        100.0 * (SELECT count(*) FROM per_reservation WHERE stored_rows > 1)
        / nullif((SELECT count(*) FROM per_reservation), 0)
    , 2)                                                          AS percent_affected,
    (SELECT sum(stored_rows) FROM per_reservation)                AS old_booking_count,
    (SELECT count(*) FROM per_reservation)                        AS new_booking_count,
    (SELECT sum(true_los) FROM per_reservation)                   AS old_room_nights,
    (SELECT sum(true_los) FROM per_reservation)                   AS new_room_nights,
    round(
        (SELECT sum(true_los)::numeric FROM per_reservation)
        / nullif((SELECT sum(stored_rows) FROM per_reservation), 0)
    , 3)                                                          AS old_average_los,
    round(
        (SELECT sum(true_los)::numeric FROM per_reservation)
        / nullif((SELECT count(*) FROM per_reservation), 0)
    , 3)                                                          AS new_average_los;

-- Room nights should be identical between old and new: the old query split one
-- booking into several but preserved the night total. Booking count and average
-- length of stay are the figures that were wrong.

-- The 20 reservations most distorted by the old definition.
WITH alive AS (
    SELECT fact.reservation_number, fact.enterprise_id, fact.arrival_date, fact.los
    FROM functions.reservation_los_fact fact
    WHERE fact.fact_kind = 'historical'
      AND fact.created_date <= :cutoff
      AND (fact.cancelled_date IS NULL OR fact.cancelled_date > :cutoff)
)
SELECT
    alive.reservation_number,
    hotel.hotel_name,
    alive.arrival_date,
    count(*)        AS counted_as_this_many_bookings,
    sum(alive.los)  AS true_length_of_stay,
    array_agg(alive.los ORDER BY alive.los DESC) AS reported_as_these_lengths
FROM alive
JOIN functions.hotels hotel USING (enterprise_id)
GROUP BY 1, 2, 3
HAVING count(*) > 1
ORDER BY count(*) DESC, sum(alive.los) DESC
LIMIT 20;
