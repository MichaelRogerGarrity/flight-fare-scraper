-- Example queries against the fare history.
--
-- Run them against a local DuckDB file after `cli pull`, or straight against the
-- bucket by replacing `fares` with the read_parquet(...) call in the first section.
--
-- One rule governs all of these: a single physical itinerary appears once per
-- seller, ten times on average and up to 284 times in the data collected so far.
-- MIN(price) is therefore the only honest aggregate. AVG and MEDIAN measure how
-- many resellers listed a flight, not what it costs.


---------------------------------------------------------------------------
-- Reading straight from the bucket
---------------------------------------------------------------------------

INSTALL httpfs;
LOAD httpfs;

CREATE OR REPLACE SECRET hf (
    TYPE s3,
    KEY_ID  getenv('FFS_S3_KEY_ID'),
    SECRET  getenv('FFS_S3_SECRET'),
    ENDPOINT getenv('FFS_S3_ENDPOINT'),  -- s3.hf.co/<namespace>, no scheme
    URL_STYLE 'path',                    -- required: the gateway does not serve
    REGION 'us-east-1'                   -- virtual-hosted-style bucket hostnames
);

-- Everything ever published. snapshot_date lives in the rows as well as the key
-- prefix, so hive_partitioning is unnecessary (and would collide with it).
CREATE OR REPLACE VIEW published AS
SELECT * FROM read_parquet('s3://FlightData/snapshots/**/*.parquet', union_by_name = true);

-- One day only: naming the prefix skips every other object entirely.
SELECT count(*) FROM read_parquet('s3://FlightData/snapshots/snapshot_date=2026-09-19/*.parquet');


---------------------------------------------------------------------------
-- What have I got?
---------------------------------------------------------------------------

SELECT snapshot_date,
       count(*)                                  AS rows,
       count(DISTINCT origin || '-' || destination) AS routes,
       count(DISTINCT depart_date)               AS departures_tracked
FROM fares
GROUP BY 1
ORDER BY 1;


---------------------------------------------------------------------------
-- Cheapest fare per route per day: the core time series
---------------------------------------------------------------------------

SELECT snapshot_date, origin, destination, depart_date, return_date,
       min(price) AS cheapest
FROM fares
GROUP BY ALL
ORDER BY depart_date, snapshot_date;


---------------------------------------------------------------------------
-- How one trip's price moved, day by day, with the change
---------------------------------------------------------------------------

SELECT snapshot_date,
       cheapest,
       cheapest - lag(cheapest) OVER (ORDER BY snapshot_date) AS change
FROM (
    SELECT snapshot_date, min(price) AS cheapest
    FROM fares
    WHERE origin = 'MIA' AND destination = 'MCO'   -- placeholder codes, use your own
      AND depart_date = DATE '2026-10-30' AND return_date = DATE '2026-11-02'
    GROUP BY 1
)
ORDER BY snapshot_date;


---------------------------------------------------------------------------
-- Cheapest nonstop, and cheapest under 18 hours of total travel
---------------------------------------------------------------------------

-- "Under 18 hours" means per leg, not both legs added together. The fastest single
-- leg to Tokyo is 14.1h, so a round trip is 26.6h at best -- a summed threshold of
-- 18h can never match, and quietly returns NULL instead of telling you so.
-- Note this threshold is unreachable for some routes whatever you do: the fastest
-- leg to Bangkok is 20h.
SELECT depart_date, return_date,
       min(price) FILTER (WHERE outbound_stops = 0 AND return_stops = 0) AS cheapest_nonstop,
       min(price) FILTER (WHERE outbound_duration_min <= 18 * 60
                            AND return_duration_min <= 18 * 60)          AS cheapest_under_18h_per_leg,
       min(price)                                                        AS cheapest_any
FROM fares
WHERE snapshot_date = (SELECT max(snapshot_date) FROM fares)
  AND cabin_class = 'Economy'
GROUP BY ALL
ORDER BY depart_date;


---------------------------------------------------------------------------
-- Which weekend is cheapest, across the whole rolling window (long-haul route)
---------------------------------------------------------------------------

SELECT depart_date, return_date, min(price) AS cheapest
FROM fares
WHERE origin = 'MIA' AND destination = 'SIN'   -- placeholder codes, use your own
  AND snapshot_date = (SELECT max(snapshot_date) FROM fares)
GROUP BY ALL
ORDER BY cheapest
LIMIT 10;


---------------------------------------------------------------------------
-- The real fare including bags, for the cheapest offer on each itinerary
---------------------------------------------------------------------------

SELECT origin, destination, depart_date, outbound_airline,
       min(price + coalesce(checked_bag_fee, 0) + coalesce(carry_on_fee, 0)) AS all_in
FROM fares
WHERE snapshot_date = (SELECT max(snapshot_date) FROM fares)
GROUP BY ALL
ORDER BY all_in
LIMIT 20;


---------------------------------------------------------------------------
-- Was Kayak's price prediction right? (needs a few weeks of history)
---------------------------------------------------------------------------

WITH daily AS (
    SELECT snapshot_date, depart_date, return_date,
           min(price)                        AS cheapest,
           any_value(price_prediction)       AS called
    FROM fares
    WHERE price_prediction IS NOT NULL
    GROUP BY ALL
)
SELECT daily.called,
       count(*)                                       AS calls,
       round(avg(later.cheapest - daily.cheapest), 2) AS avg_move_after
FROM daily
JOIN daily AS later USING (depart_date, return_date)
WHERE later.snapshot_date = daily.snapshot_date + INTERVAL 7 DAY
GROUP BY daily.called;
