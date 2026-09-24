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
run **one at a time** (`max-parallel: 1`): the split exists to stay under the per-job
six-hour limit, not to scrape faster. Sending several shards at a site simultaneously is
the concurrency this project deliberately avoids — searches are already spaced with random
jitter for the same reason.

Each shard writes to a throwaway local DuckDB file and publishes its rows to object
storage, so no state has to survive the runner.

A shard tolerates up to three failed searches before it goes red: at ~92 searches a
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

### Setting up Hugging Face storage

1. Create a bucket at [huggingface.co/new-bucket](https://huggingface.co/new-bucket) and
   mark it **private**. A free account includes 100 GB of private storage.
2. At [Access Tokens](https://huggingface.co/settings/tokens), create a **Write** token
   scoped to that bucket, then use its dropdown menu → **Generate S3 credentials**. The
   secret is shown once.
3. Put the values into the repository secrets above.

Any S3-compatible store works — the endpoint just has to be given without a scheme.
DuckDB's `httpfs` extension does the reading and writing, so there is no extra dependency.

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
