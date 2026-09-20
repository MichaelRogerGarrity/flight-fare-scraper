import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional, Tuple

from . import db, schedule, storage
from .config import build_query, load_queries
from .models import SearchQuery
from .output import write_results
from .runner import run_queries

logger = logging.getLogger("flight_fare_scraper")

CONFIG_HELP = (
    "CSV with columns origin,destination,depart,return_date and optionally "
    "nonstop,outbound_takeoff,return_takeoff,outbound_landing,return_landing,site"
)
SPEC_HELP = "JSON route spec expanded into the searches due today (see examples/routes.spec.example.json)"
SPEC_ENV_HELP = "Name of an environment variable holding the route spec JSON, e.g. FFS_ROUTES"
TAKEOFF_HELP = "HHMM,HHMM on this leg's departure date, e.g. 1600,2300"
LANDING_HELP = "HHMM,HHMM; prefix a bound with +N@ to land N days after this leg departs, e.g. +1@1200,+1@2359"
SHARD_HELP = "Run only part of today's searches, as i/n (e.g. 0/3), to split a run across jobs"
LOG_FILE_HELP = (
    "Log file; always receives debug-level detail. Pass an empty string to log only to the "
    "console, which is what a public CI run should do -- debug lines carry search URLs."
)


def configure_logging(verbose: bool, log_file: Optional[str]) -> None:
    handlers: List[logging.Handler] = []
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handlers.append(file_handler)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handlers.append(console)
    logger.setLevel(logging.DEBUG)
    logger.handlers[:] = handlers
    logger.propagate = False


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", help=CONFIG_HELP)
    source.add_argument("--spec", help=SPEC_HELP)
    source.add_argument("--spec-env", dest="spec_env", help=SPEC_ENV_HELP)
    parser.add_argument("--today", help="Treat this YYYY-MM-DD as today when expanding a route spec")
    parser.add_argument("--shard", help=SHARD_HELP)


def parse_shard(text: Optional[str]) -> Optional[Tuple[int, int]]:
    if not text:
        return None
    parts = text.split("/")
    if len(parts) != 2:
        raise ValueError(f"--shard must look like i/n, got {text!r}")
    try:
        index, count = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"--shard must look like i/n, got {text!r}") from None
    return index, count


def queries_from(args: argparse.Namespace) -> List[SearchQuery]:
    """Build the search list from a CSV config, a route spec file, or a spec in the environment."""
    if args.config:
        return load_queries(args.config)

    if args.spec:
        text = Path(args.spec).read_text(encoding="utf-8")
    else:
        text = os.environ.get(args.spec_env, "")
        if not text.strip():
            raise ValueError(f"environment variable {args.spec_env} is empty or unset")

    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()
    queries = schedule.due_today(schedule.parse_spec(text), today)
    shard = parse_shard(args.shard)
    if shard:
        queries = schedule.shard(queries, *shard)
    return queries


def build_arg_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--headed", action="store_true", help="Run with a visible browser window (for debugging)")
    common.add_argument("--verbose", action="store_true", help="Show debug-level logs on the console")
    common.add_argument("--log-file", default="logs/flight_fare_scraper.log", help=LOG_FILE_HELP)
    common.add_argument("--max-pages", type=int, default=None, dest="max_pages",
                        help="Result pages to fetch per pass (default 20). Lower it for routine tracking: "
                             "the cheapest fares sit on the first pages once results are price-sorted.")

    parser = argparse.ArgumentParser(description="Scrape round-trip fares (1 adult) from Kayak and future sites.")
    sub = parser.add_subparsers(dest="mode", required=True)

    single = sub.add_parser("search", parents=[common], help="Run a single search")
    single.add_argument("--origin", required=True, help="Origin airport or metro code")
    single.add_argument("--destination", required=True, help="Destination airport or metro code")
    single.add_argument("--depart", required=True, help="Departure date YYYY-MM-DD")
    single.add_argument("--return-date", required=True, dest="return_date", help="Return date YYYY-MM-DD")
    single.add_argument("--allow-stops", action="store_true", help="Include connecting flights (default: nonstop only)")
    single.add_argument("--outbound-takeoff", default="", help=TAKEOFF_HELP)
    single.add_argument("--return-takeoff", default="", help=TAKEOFF_HELP)
    single.add_argument("--outbound-landing", default="", help=LANDING_HELP)
    single.add_argument("--return-landing", default="", help=LANDING_HELP)
    single.add_argument("--site", default="kayak", help="Site to scrape (default: kayak)")
    single.add_argument("--output", default="results.csv", help="Output file: .csv, .json, or .xlsx")

    batch = sub.add_parser("batch", parents=[common], help="Run many searches and write them to a file")
    add_source_arguments(batch)
    batch.add_argument("--output", default="results.csv", help="Output file: .csv, .json, or .xlsx")

    track = sub.add_parser("track", parents=[common],
                           help="Run searches and record a dated snapshot into DuckDB")
    add_source_arguments(track)
    track.add_argument("--db", default="fares.duckdb", help="DuckDB database file (default: fares.duckdb)")
    track.add_argument("--push", action="store_true",
                       help="Also publish this snapshot to object storage (see FFS_S3_* in the README)")
    track.add_argument("--run-id", dest="run_id", default="",
                       help="Name for the published object; defaults to the snapshot timestamp")
    track.add_argument("--summary", default="",
                       help="Write a counts-only JSON summary of this run here. Safe to publish: "
                            "it holds no routes, prices or error messages.")

    runlog = sub.add_parser("runlog", help="Append counts-only run summaries to a CSV log")
    runlog.add_argument("--summaries", required=True, help="Directory of summary JSON files to fold in")
    runlog.add_argument("--out", default="run-log.csv", help="CSV log to append to (created if absent)")

    plan = sub.add_parser("plan", help="Print what a route spec would run today, without scraping")
    add_source_arguments(plan)
    plan.add_argument("--show-routes", action="store_true",
                      help="List each search. Prints real airport codes, so not for a public log.")

    pull = sub.add_parser("pull", parents=[common],
                          help="Merge published snapshots from object storage into a local DuckDB file")
    pull.add_argument("--db", default="fares.duckdb", help="DuckDB database file to merge into")

    return parser


RUN_LOG_FIELDS = (
    "run_date", "run_id", "shard", "searches", "succeeded", "failed",
    "bot_blocked", "rows", "max_pages", "seconds", "error_types",
)


def summarize(report, queries: List[SearchQuery], args: argparse.Namespace,
              rows: int, seconds: float, run_date: str) -> dict:
    """Counts and exception types only.

    Deliberately no route, price or error text: this summary is committed to a
    public repo, and a failure message can carry the URL the site redirected to.
    """
    return {
        "run_date": run_date,
        "run_id": args.run_id or "",
        "shard": args.shard or "1/1",
        "searches": len(queries),
        "succeeded": len(report.succeeded),
        "failed": len(report.failures),
        "bot_blocked": sum(failure.bot_blocked for failure in report.failures),
        "rows": rows,
        "max_pages": args.max_pages if args.max_pages is not None else "",
        "seconds": round(seconds),
        "error_types": " ".join(sorted({
            failure.error.split(":", 1)[0].strip() for failure in report.failures
        })),
    }


def run_track(args: argparse.Namespace, queries: List[SearchQuery]) -> int:
    started = time.monotonic()
    report = run_queries(queries, headless=not args.headed, max_pages=args.max_pages)
    con = db.connect(args.db)
    recorded = 0  # bound before the try so the summary still gets written if recording fails
    try:
        moment = db.snapshot_now()
        recorded = db.insert_snapshot(con, report.results, moment)
        logger.info("recorded %d rows into %s", recorded, args.db)
        if args.push and recorded:
            bucket = storage.require_env(storage.BUCKET_ENV)
            storage.configure(con)
            run_id = args.run_id or moment.strftime("%Y%m%dT%H%M%S")
            storage.push_snapshot(
                con, storage.uri(bucket, storage.snapshot_key(moment.date(), run_id)), moment.date()
            )
    finally:
        con.close()
        if args.summary:
            summary = summarize(report, queries, args, recorded,
                                time.monotonic() - started, db.snapshot_now().date().isoformat())
            Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
            Path(args.summary).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            logger.info("wrote run summary to %s", args.summary)
    return 1 if report.failures else 0


def run_runlog(args: argparse.Namespace) -> int:
    """Fold each shard's summary into one CSV. Committing it is also what keeps the
    repository from looking idle, which would have GitHub disable the schedule."""
    summaries = sorted(Path(args.summaries).glob("**/*.json"))
    if not summaries:
        logger.error("no summary files under %s", args.summaries)
        return 2
    out = Path(args.out)
    has_rows = out.exists() and out.stat().st_size > 0
    # Keyed so re-running the log job doesn't double-enter a run that is already there.
    seen = set()
    if has_rows:
        with out.open(newline="", encoding="utf-8") as handle:
            seen = {(row["run_date"], row["run_id"], row["shard"]) for row in csv.DictReader(handle)}

    appended = 0
    with out.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUN_LOG_FIELDS, extrasaction="ignore")
        if not has_rows:
            writer.writeheader()
        for path in summaries:
            row = json.loads(path.read_text(encoding="utf-8"))
            key = (str(row.get("run_date", "")), str(row.get("run_id", "")), str(row.get("shard", "")))
            if key in seen:
                continue
            seen.add(key)
            writer.writerow({field: row.get(field, "") for field in RUN_LOG_FIELDS})
            appended += 1
    logger.info("appended %d of %d run(s) to %s", appended, len(summaries), out)
    return 0


def run_plan(args: argparse.Namespace, queries: List[SearchQuery]) -> int:
    """Counts only by default: `plan` is safe to run in a public log, --show-routes is not."""
    print(f"{len(queries)} search(es) due")
    if args.show_routes:
        for query in queries:
            print(f"  {query.label}{'' if query.nonstop_only else '  (stops allowed)'}")
        return 0

    by_route: dict = {}
    for query in queries:
        key = (query.origin, query.destination)
        by_route[key] = by_route.get(key, 0) + 1
    for position, count in enumerate(sorted(by_route.values(), reverse=True), start=1):
        print(f"  route {position}: {count}")
    return 0


def run_pull(args: argparse.Namespace) -> int:
    con = db.connect(args.db)
    try:
        storage.configure(con)
        storage.pull(con, storage.require_env(storage.BUCKET_ENV), db.COLUMNS,
                     ["snapshot_date", *db.ROUTE_KEY])
    finally:
        con.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Exit codes: 0 all searches succeeded, 1 some failed (see logs), 2 invalid configuration."""
    args = build_arg_parser().parse_args(argv)
    configure_logging(getattr(args, "verbose", False), getattr(args, "log_file", None))

    try:
        if args.mode == "pull":
            return run_pull(args)
        if args.mode == "runlog":
            return run_runlog(args)

        if args.mode == "search":
            queries = [build_query(
                args.origin, args.destination, args.depart, args.return_date,
                nonstop="false" if args.allow_stops else "true",
                outbound_takeoff=args.outbound_takeoff, return_takeoff=args.return_takeoff,
                outbound_landing=args.outbound_landing, return_landing=args.return_landing,
                site=args.site,
            )]
        else:
            queries = queries_from(args)
    except (ValueError, OSError, storage.StorageError) as error:
        logger.error("invalid configuration: %s", error)
        return 2

    if args.mode == "plan":
        return run_plan(args, queries)
    if not queries:
        logger.info("nothing is due today")
        return 0

    if args.mode == "track":
        try:
            return run_track(args, queries)
        except storage.StorageError as error:
            # The scrape itself succeeded; only publishing failed, and the rows are in the local DB.
            logger.error("PUBLISH_FAILED: %s", error)
            return 1

    report = run_queries(queries, headless=not args.headed, max_pages=args.max_pages)
    write_results(report.results, args.output)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
