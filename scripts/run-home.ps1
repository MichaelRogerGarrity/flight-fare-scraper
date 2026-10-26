# Daily tracking run from a home machine, for when the hosted runners can't reach the site.
#
# Uses the same code and publishes to the same bucket, under the same keys, as the
# scheduled workflow -- through this machine's Hugging Face login (`hf auth login`), so no
# storage keys are needed here. The route list and bucket name live only on this machine.
#
# Run by a Windows scheduled task that starts as soon as it can after a missed start, so
# the run may begin any time of day, and again at each logon: a run cut short by a
# shutdown picks up where it stopped (--resume), and a day already done exits at once.
# See the README ("Running from a home machine").

param(
    # The route spec. Gitignored: it names the routes actually tracked.
    [string]$Spec = "routes.spec.json",
    # namespace/bucket. Falls back to FFS_HF_BUCKET if already set in the environment.
    [string]$Bucket = $env:FFS_HF_BUCKET,
    # Spread the searches to end about this many hours after starting.
    [double]$TargetHours = 5,
    # Start no search more than this many hours after starting, so a run held up by sleep
    # or a slow site can't run into the next day's.
    [double]$DeadlineHours = 20,
    # Don't start a day's run before this local time, so an early logon waits for the
    # scheduled start rather than beginning the day early.
    [string]$NotBefore = "09:00",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force "home-runs" | Out-Null

if (-not $Bucket) { throw "No bucket: pass -Bucket namespace/name or set FFS_HF_BUCKET" }
if (-not (Test-Path $Spec)) { throw "Route spec not found: $Spec" }
$env:FFS_HF_BUCKET = $Bucket

$now = Get-Date
if ($now.TimeOfDay -lt [timespan]::Parse($NotBefore)) { exit 0 }
$runId = "home-" + $now.ToUniversalTime().ToString("yyyyMMddTHHmmss")
$utc = { param($t) $t.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss") + "+00:00" }

# One process does the whole day here, so pacing has no later shards to leave room for.
# The snapshot date is pinned to the day the run started, even if it finishes after midnight.
& $Python -m flight_fare_scraper.cli track `
    --spec $Spec `
    --db "home-runs/home.duckdb" `
    --run-id $runId `
    --snapshot-date $now.ToString("yyyy-MM-dd") `
    --resume `
    --publish-hf `
    --pace-until (& $utc $now.AddHours($TargetHours)) `
    --searches-after 0 `
    --job-hours $TargetHours `
    --deadline (& $utc $now.AddHours($DeadlineHours)) `
    --summary "home-runs/summary-$runId.json" `
    --log-file "home-runs/home.log" *> "home-runs/run-$runId.txt"

exit $LASTEXITCODE
