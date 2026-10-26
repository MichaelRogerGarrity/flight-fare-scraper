# flight-fare-scraper

Tracks round-trip airfares over time. It drives a real Chrome via Playwright, reads
Kayak's internal results API, and records every offer into DuckDB so you can ask how a
fare moved rather than only what it costs today.

A scheduled GitHub Actions workflow runs it daily and publishes each snapshot as a
Parquet object to S3-compatible storage, so nothing depends on a desktop being awake.

## What a row is

One row is **one booking option for one round trip, as seen on one day**: a seller's
price for a specific pair of legs. The same itinerary usually appears many times over
from different sellers, so `MIN(price)` is the honest aggregate — `AVG` and `MEDIAN` are
weighted by how many resellers happened to list a flight.

Each row carries both legs in full (airline, operating carrier, times, stops, duration,
layover airports and minutes, airport change, equipment), the cabin, bag fees as separate
fields from the fare, cancellation and virtual-interline flags, and Kayak's own price
prediction. `snapshot_date` is stamped in US Eastern wall-clock time.

`origin` and `destination` are what was *searched*, often a metro code (`NYC` covers
JFK, LGA and EWR). The airports actually flown are in `outbound_from`, `outbound_to`,
`return_from` and `return_to`, recorded from 2026-09-25 on. Note that a metro search
only shows an airport if its fares rank: with results price-sorted and five pages
fetched, an airport with few and expensive options can be included in the search yet
never appear. Search that airport as its own route to guarantee it is tracked.

Departure and arrival timestamps are local wall-clock at each airport, so an eastbound
transpacific leg can land "before" it took off. Use `*_duration_min` for trip length,
never arrival minus departure.

## Running it locally

```bash
pip install -r requirements.txt
python -m flight_fare_scraper.cli search --origin SEA --destination ORD \
  --depart 2026-10-30 --return-date 2026-11-02 --output results.csv
```

Record a dated snapshot into DuckDB instead of a file:

```bash
python -m flight_fare_scraper.cli track --config routes.csv --db fares.duckdb
```

Pull down everything the scheduled runs have published:

```bash
python -m flight_fare_scraper.cli pull --db fares.duckdb
```

Same-day re-runs replace that day's rows for the same route rather than duplicating them,
so `track` and `pull` are both safe to repeat.

## The route spec

Routes are described by trip *shape*, not by date:

```json
{ "origin": "SEA", "destination": "ORD", "nights": 2, "nonstop": true }
```

`schedule.py` expands that over a rolling horizon of base weekends (Fridays by default)
and scans the ones that are due today. Tiers decide how often a weekend comes up and how
many date combinations it gets — near dates daily with a day of slack at each end (nine
combinations), far dates weekly with just the base weekend. Because "due" is a function of
how far out a departure is, a missed run costs nothing: that weekend simply waits for its
next turn.

Because the slack applies to both ends, a short trip's grid includes stays shorter than
intended -- a 2-night weekend can come out as leaving a day late and returning a day
early, the same day. `min_nights` (default 1) drops anything shorter, so same-day round
trips are never searched.

A route can also set its own `base_weekday` -- a number (Monday is 0) or a name such as
`"Tuesday"` -- to track a different trip shape beside the default. Listing the same route
twice, once on Friday with 16 nights and once on Tuesday with 15, tracks both a Friday-to-
Sunday and a Tuesday-to-Wednesday fortnight. The two never produce the same search: a day
of slack around Friday departs Thursday to Saturday, around Tuesday Monday to Wednesday.

**Rotating the date grid.** A tier can set `combos_per_visit`. Each visit then searches
the base trip plus a rotating handful of the other date combinations, so every weekend is
still priced every visit while the whole grid is covered every few visits. The grid is
most of the daily volume -- each near-term weekend is 8 or 9 searches without it -- so this
is the main lever on how many searches a day a spec costs.

**No intervals in whole weeks.** `every_days` may not be a multiple of 7: that would check
each window on the same weekday every time, and any effect of the day a fare is searched
would hide in the data. Any other interval rotates through all seven weekdays.

**Check a spec's load before using it:**

```bash
python -m flight_fare_scraper.cli budget --spec routes.spec.json --limit 400
```

simulates every day of the next year with the real scheduler and reports searches per day
(average and worst day), page requests, runtime, how the worst day splits by tier and
route, and whether any window is stuck on one weekday. It exits 1 if any day exceeds
`--limit`. `--costs` calibrates each route's cost from the per-search records. The worst
day is the figure that matters: on 2026-10-22 a spec that doubled daily searches to ~780
got the runners blocked within a day, after a month at ~400 had been clean.

See [examples/routes.spec.example.json](examples/routes.spec.example.json) for the shape.
Check what a spec would do before running it:

```bash
python -m flight_fare_scraper.cli plan --spec routes.spec.json
```

**The real spec is not in this repo.** It names the airports actually being tracked, so it
lives only in the `FFS_ROUTES` secret. Don't commit a filled-in copy — `routes.spec.json`
is gitignored to make that harder to do by accident.

## Scheduled runs

`.github/workflows/track.yml` runs daily. It splits the day's searches into shards that
run **one at a time** (`max-parallel: 1`) — never in parallel, which is the concurrency
this project deliberately avoids; searches are already spaced with random jitter for the
same reason.

The split exists because the site quietly stops answering a runner after roughly 110–125
searches' worth of requests. There is no redirect to a bot page: from that point every
attempt times out waiting for page 1, and relaunching the browser doesn't help because the
runner's address is unchanged. Each shard gets a fresh runner, so a `plan` job counts the
day's searches first and sizes the run at no more than 70 per shard. Adding routes adds
shards rather than pushing any runner toward the wall. `plan` also fixes the date once, so
shards that run past midnight UTC still expand the same day's schedule.

`plan` also fixes the run's snapshot date once, and every shard records against it. With
the start delay, a full run can finish late in the Eastern evening, and a shard stamping
its own date from the clock could put part of one run on the next day.

Each route is dealt out across the shards evenly, so no shard ends up with all the
long-haul searches, which make several times more requests than a domestic one.

Each shard writes to a throwaway local DuckDB file and publishes its rows to object
storage, so no state has to survive the runner.

A search whose later pages won't load keeps the pages it already has rather than
failing. Results are price-sorted, so page 1 alone holds the cheapest fares; the run log's
`partial` column counts how many searches were cut short this way. The usual cause is
something drawn over the "Show more results" button on one runner -- the scraper first
retries the click from inside the page, which ignores overlays, and if that fails the
warning it logs says what host and language were served and what was covering the button.

**Pacing.** A run spreads its searches across the day instead of firing them back to
back. `plan` sets a target end, 14 hours after the run starts by default and always an hour
before the deadline, and each shard spaces its searches to take its share of the time left
-- given the searches still to come in later shards -- after subtracting what searches are
measured to take. It re-measures as it goes, so a slow shard leaves later ones pacing faster
instead of overrunning, and no shard paces itself past five hours, under the job limit. Each
pause is randomised around the computed figure. With ~250 searches a day that is about two
minutes between searches rather than 5-20 seconds. Whether the site's tolerance is per hour
or per day isn't known; if it is per hour this is gentler, and if per day it costs nothing.
A manual run can set its own target with the `target_hours` input; smoke tests (`limit`)
aren't paced. `cli budget` reports the spacing a spec would get.

**The circuit breaker.** A shard stops after five searches fail in a row -- at the usual
~1% failure rate that is not chance, it is a block, and retrying into a block only
prolongs it. Searches it didn't reach are recorded as skipped, and later shards check the
earlier ones' summaries and skip themselves rather than rediscover the block on fresh
runners. Separately, no search starts after a deadline set by `plan`: 30 minutes before the
next scheduled run, so one day's run can never overlap the next. Either way the run goes red,
and the run log's `skipped` and `stopped` columns say how much was left undone and why.

A shard tolerates up to three failed searches before it goes red: at ~70 searches a
shard, one transient timeout is noise, and a daily unattended job that cries wolf gets
ignored. Any bot-block fails the run regardless of the tolerance, since that is the
signal worth reacting to. Every failure is recorded in the run log either way.

### The run log, and why it exists

After the shards finish, one more job folds their counts into [`run-log.csv`](run-log.csv)
and commits it — one row per shard per day, with searches attempted and succeeded, rows
recorded, bot-blocks, elapsed seconds, and the exception types seen. Counts and type names
only: no routes, prices, URLs or error text, because a failure message can carry the URL
the site redirected to.

That commit does double duty. GitHub disables scheduled workflows in a **public** repo
after 60 days with no repository activity, and workflow runs don't count as activity —
only commits do. A tracker that writes its data to object storage never touches git, so
without this it would go quiet around day 60 with no failure and no notification.

GitHub has never documented whether a commit made with `GITHUB_TOKEN` resets that timer;
the bot-commit approach is community practice, not a guarantee. If you want certainty, add
a personal access token as `FFS_KEEPALIVE_TOKEN` and the job will push with it instead.

### Secrets

| Secret | What it is |
| --- | --- |
| `FFS_ROUTES` | The route spec JSON, pasted whole |
| `FFS_REDACT_SALT` | Any random string; salts the route ids that appear in logs |
| `FFS_S3_ENDPOINT` | `s3.hf.co/<your-hf-username>` — no scheme |
| `FFS_S3_BUCKET` | The bucket name alone, without the namespace |
| `FFS_S3_KEY_ID` | S3 access key id, starts with `HFAK` |
| `FFS_S3_SECRET` | S3 secret access key |
| `FFS_KEEPALIVE_TOKEN` | Optional PAT for the run-log commit (see above) |

The same four `FFS_S3_*` values work as local environment variables for `pull`.

**Store `FFS_ROUTES` as a single line.** GitHub masks every line of a multi-line secret
separately, so an indented spec with `1` or `0` on a line of its own gets those
characters replaced by `***` in every log the repo produces -- counts, shard numbers,
route ids and all. Compact it on the way in:

```bash
python -c "import json; print(json.dumps(json.load(open('routes.spec.json')), separators=(',', ':')), end='')" | gh secret set FFS_ROUTES
```

### Setting up Hugging Face storage

1. Create a bucket at [huggingface.co/new-bucket](https://huggingface.co/new-bucket) and
   mark it **private**. A free account includes 100 GB of private storage.
2. At [Access Tokens](https://huggingface.co/settings/tokens), create a **Write** token
   scoped to that bucket, then use its dropdown menu → **Generate S3 credentials**. The
   secret is shown once.
3. Put the values into the repository secrets above.

Any S3-compatible store works — the endpoint just has to be given without a scheme.
DuckDB's `httpfs` extension does the reading and writing, so there is no extra dependency.

### Per-search records

Public logs name routes only by salted id, which is enough to see that a search failed
but not which one. So every search also leaves a record in the private bucket under
`diagnostics/`, beside the fares and partitioned the same way:

- what was searched: route, dates, days out
- how it went: `ok`, `partial` or `failed`, attempts, and the full error text
- how long it took: total seconds, and seconds until page 1 answered, for the main search
  and the nonstop pass separately
- how much was fetched: pages fetched against pages available, results the site reported,
  rows kept and rows dropped by the stops and time filters
- warning signs: in-page clicks past a covered button, and poll responses the parser
  didn't recognize -- a count that rises alongside timeouts is what a change to the
  site's response format would look like

They carry real routes and error text, so they go only to the private bucket and are
never printed or uploaded with the public run. Locally, `track --diagnostics FILE` writes
the same records to a Parquet file. A record is written even for a search that failed on
every attempt, which is when it is most useful.

```sql
-- Which routes fail, and how
SELECT origin, destination, error_type, count(*) AS searches
FROM read_parquet('s3://<bucket>/diagnostics/**/*.parquet')
WHERE outcome <> 'ok' GROUP BY ALL ORDER BY searches DESC;

-- Page-1 latency per day: a slow creep toward the 45s timeout comes before failures
SELECT snapshot_date, round(median(first_page_seconds), 1) AS median_s,
       round(quantile_cont(first_page_seconds, 0.95), 1) AS p95_s
FROM read_parquet('s3://<bucket>/diagnostics/**/*.parquet') GROUP BY 1 ORDER BY 1;

-- What the page cap leaves on the table
SELECT origin, destination, avg(pages_available) AS available, avg(pages_fetched) AS fetched
FROM read_parquet('s3://<bucket>/diagnostics/**/*.parquet') GROUP BY ALL;
```

### Each run's page

The log step also writes this run's shards as a table on the run's page in GitHub
Actions: searches, failures, blocks, partial searches, in-page clicks, rows and minutes
per shard, with a shard that never ran called out. Counts only, like the run log.
The run log itself gains `in_page_clicks`, `start_delay_min` -- how many minutes after
its scheduled time the run actually started, since GitHub fires scheduled runs late --
and `skipped` and `stopped`, for a run the circuit breaker or the deadline cut short.

### The weekly audit

`.github/workflows/audit.yml` reads the published objects in place every Tuesday and
reports coverage against the schedule, columns gone empty on the latest snapshot, and
implausible prices, durations and timestamps. It checks coverage over the last 7 days
only: a short day stays short forever, so checking all history would fail every audit
after one bad day. With a weekly audit, a 7-day window checks each day exactly once.

## Keeping the public log clean

This repo is public; its Actions logs are too. Two things keep routes out of them:

- `FFS_REDACT_LOGS=1` replaces route labels with salted digests (`route-3f2a91c4b0de`).
  Without a salt the digest is brute-forceable, since airport pairs and dates are a small
  space — so set `FFS_REDACT_SALT` as well.
- `--log-file ""` disables the debug log entirely on CI runners. Debug lines carry the
  full search URL, airport codes and all, so on CI that file is never written at all.
- Error messages are reduced to their exception type when redaction is on, since a
  bot-block reports the URL it was redirected to.

The only artifact a scheduled run uploads is each shard's counts-only summary.

`plan` prints counts only unless you pass `--show-routes`, which prints real airport codes
and is meant for a terminal, not a log.

## Querying the data

A "shard" is a job split, not a data dimension: each scheduled run divides that day's
search list into three, and each shard writes its own Parquet object. Nothing about a
shard is meaningful once the rows are loaded -- the objects are just however the day's
rows happened to be cut up.

Objects are keyed `snapshots/snapshot_date=YYYY-MM-DD/<run-id>-shard<n>.parquet`, so a
query for one day can name the prefix and skip everything else.

Easiest path is to pull them into a local file and query that:

```bash
python -m flight_fare_scraper.cli pull --db fares.duckdb
```

Or read the bucket in place, without downloading anything first:

```sql
INSTALL httpfs; LOAD httpfs;
CREATE OR REPLACE SECRET hf (
    TYPE s3, KEY_ID getenv('FFS_S3_KEY_ID'), SECRET getenv('FFS_S3_SECRET'),
    ENDPOINT getenv('FFS_S3_ENDPOINT'), URL_STYLE 'path', REGION 'us-east-1');

SELECT snapshot_date, min(price)
FROM read_parquet('s3://<bucket>/snapshots/**/*.parquet', union_by_name = true)
GROUP BY 1 ORDER BY 1;
```

`URL_STYLE 'path'` is required — without it DuckDB tries `<bucket>.s3.hf.co`, which the
gateway doesn't serve.

[examples/queries.sql](examples/queries.sql) has worked examples: price history for one
itinerary, cheapest nonstop versus cheapest under 18 hours, all-in cost with bag fees, and
whether Kayak's own predictions held up.

## Adding another site

Implement `BaseScraper` in `flight_fare_scraper/scrapers/`, register it, and set `site` on
a query. `site` is part of the dedup key, so two scrapers can track the same route on the
same day without overwriting each other.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The Kayak parser tests run against a real (trimmed) API response in `tests/fixtures/`,
with airport codes replaced by placeholders.
