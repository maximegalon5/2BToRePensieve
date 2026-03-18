# YouTube Daily Sync — runs via Windows Task Scheduler
# Uses local home IP to avoid YouTube cloud-IP blocking
#
# Setup:
#   1. Edit $PlaylistUrl below with your playlist URL
#   2. Register with Task Scheduler (see README for commands)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$LogDir = Join-Path $RepoRoot "logs"
$LogFile = Join-Path $LogDir ("youtube-sync-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

# --- CONFIGURE THIS ---
$PlaylistUrl = "https://www.youtube.com/playlist?list=YOUR_PLAYLIST_ID"

# Ensure logs dir exists
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# Fallback to system Python if venv doesn't exist
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content $LogFile "`n=== YouTube Sync started at $timestamp ==="

try {
    & $Python -m open_brain.connectors.youtube `
        --playlist $PlaylistUrl `
        --sync `
        --limit 10 `
        --delay 2.0 `
        2>&1 | Tee-Object -Append -FilePath $LogFile

    $exitCode = $LASTEXITCODE
    $status = if ($exitCode -eq 0) { "SUCCESS" } else { "FAILED (exit $exitCode)" }
} catch {
    $status = "ERROR: $_"
    Add-Content $LogFile $status
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content $LogFile "=== YouTube Sync finished at $timestamp — $status ==="
