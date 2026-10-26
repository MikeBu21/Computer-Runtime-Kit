# Register a Windows Scheduled Task that keeps TailscaleNotify.ps1 running.
# Run in elevated PowerShell once on cad2020 (or any Windows Tailscale host).
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\TailscaleNotify"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Force "$PSScriptRoot\TailscaleNotify.ps1" "$InstallDir\TailscaleNotify.ps1"
if (-not (Test-Path "$InstallDir\config.json")) {
    Copy-Item "$PSScriptRoot\config.json.example" "$InstallDir\config.json"
    Write-Host "Edit $InstallDir\config.json and set telegram_token + chat_ids"
}

$taskName = "TailscaleTelegramNotify"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$InstallDir\TailscaleNotify.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Notify Telegram when Tailscale IP/state changes" -Force | Out-Null

# Immediate test send
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$InstallDir\TailscaleNotify.ps1" -Force
Write-Host "Installed task '$taskName'. Start it from Task Scheduler or sign out/in."
