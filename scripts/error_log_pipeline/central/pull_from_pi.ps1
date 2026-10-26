# Pull pending weekly error packages from piMiner over Tailscale/SSH into logs/pi/YYYY-MM-DD
# Then start (or reuse) the local viewer on port 9123.
param(
    [string]$PiHost = "piminer@100.105.158.95",
    [string]$RemoteOutbox = "/home/piminer/minersteuerung/logs/error_reports/outbox",
    [string]$ReceiveRoot = "$PSScriptRoot\data",
    [int]$Port = 9123
)

$ErrorActionPreference = "Stop"
$piRoot = Join-Path $ReceiveRoot "logs\pi"
New-Item -ItemType Directory -Force -Path $piRoot | Out-Null

$tmp = Join-Path $env:TEMP ("pi-errors-" + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    scp -o BatchMode=yes -o ConnectTimeout=15 "${PiHost}:${RemoteOutbox}/piMiner-errors-*" "$tmp/" 2>$null
} catch {
    Write-Host "scp note: $_"
}

Get-ChildItem $tmp -Filter "piMiner-errors-*.meta.json" | ForEach-Object {
    $meta = Get-Content -Raw $_.FullName | ConvertFrom-Json
    $week = $meta.week_label
    if (-not $week) { $week = "unknown" }
    $dest = Join-Path $piRoot $week
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $base = $_.BaseName  # piMiner-errors-YYYY-MM-DD.meta
    $label = $base -replace '\.meta$',''
    Copy-Item -Force $_.FullName (Join-Path $dest $_.Name)
    $gz = Join-Path $tmp ($label + ".jsonl.gz")
    if (Test-Path $gz) { Copy-Item -Force $gz (Join-Path $dest (Split-Path $gz -Leaf)) }
    Write-Host "imported week $week"
}

# Start viewer if not listening
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $py) { throw "Python not found — install Python 3 to run server.py" }
    $env:ERROR_LOG_RECEIVE_ROOT = $ReceiveRoot
    $env:ERROR_LOG_PORT = "$Port"
    Start-Process -FilePath $py.Source -ArgumentList "`"$PSScriptRoot\server.py`"" -WindowStyle Hidden
    Start-Sleep -Seconds 1
}

Write-Host "Open http://127.0.0.1:$Port/"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
