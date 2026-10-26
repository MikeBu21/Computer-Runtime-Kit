# Lightweight Tailscale → Telegram notifier for Windows (PowerShell 5+)
# Polls every IntervalSec; sends only on change.
param(
    [string]$ConfigPath = "$PSScriptRoot\config.json",
    [string]$StatePath = "$PSScriptRoot\state.json",
    [switch]$Once,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Get-Config {
    if (-not (Test-Path $ConfigPath)) {
        throw "Missing config: $ConfigPath"
    }
    $cfg = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json
    if (-not $cfg.telegram_token -and $env:TELEGRAM_BOT_TOKEN) {
        $cfg | Add-Member -NotePropertyName telegram_token -NotePropertyValue $env:TELEGRAM_BOT_TOKEN -Force
    }
    if ((-not $cfg.chat_ids -or $cfg.chat_ids.Count -eq 0) -and $env:TELEGRAM_CHAT_IDS) {
        $ids = @()
        foreach ($p in ($env:TELEGRAM_CHAT_IDS -split ",")) {
            if ($p.Trim()) { $ids += [int]$p.Trim() }
        }
        $cfg | Add-Member -NotePropertyName chat_ids -NotePropertyValue $ids -Force
    }
    return $cfg
}

function Invoke-Tailscale([string[]]$Args) {
    $ts = Get-Command tailscale -ErrorAction SilentlyContinue
    if (-not $ts) {
        $cand = @(
            "$env:ProgramFiles\Tailscale\tailscale.exe",
            "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($cand) { $ts = $cand } else { return @{ Code = 127; Out = "tailscale not found" } }
    } else {
        $ts = $ts.Source
    }
    $p = Start-Process -FilePath $ts -ArgumentList $Args -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput "$env:TEMP\ts_out.txt" -RedirectStandardError "$env:TEMP\ts_err.txt"
    $out = ""
    if (Test-Path "$env:TEMP\ts_out.txt") { $out = (Get-Content -Raw "$env:TEMP\ts_out.txt").Trim() }
    if (-not $out -and (Test-Path "$env:TEMP\ts_err.txt")) {
        $out = (Get-Content -Raw "$env:TEMP\ts_err.txt").Trim()
    }
    return @{ Code = $p.ExitCode; Out = $out }
}

function Get-Snapshot([string]$Hostname) {
    $ipv4 = ""
    $ipv6 = ""
    $r4 = Invoke-Tailscale @("ip", "-4")
    if ($r4.Code -eq 0 -and $r4.Out) { $ipv4 = ($r4.Out -split "`n")[0].Trim() }
    $r6 = Invoke-Tailscale @("ip", "-6")
    if ($r6.Code -eq 0 -and $r6.Out) {
        $first = ($r6.Out -split "`n")[0].Trim()
        if ($first -and -not $first.ToLower().StartsWith("error")) { $ipv6 = $first }
    }
    $backend = "unknown"
    $online = $null
    $dns = ""
    $keyExp = ""
    $health = @()
    $rj = Invoke-Tailscale @("status", "--json")
    if ($rj.Code -eq 0 -and $rj.Out.StartsWith("{")) {
        $data = $rj.Out | ConvertFrom-Json
        $backend = [string]$data.BackendState
        if ($data.Health) { $health = @($data.Health) }
        $self = $data.Self
        if ($self) {
            $online = $self.Online
            $dns = ([string]$self.DNSName).TrimEnd(".")
            $keyExp = [string]$self.KeyExpiry
            if (-not $Hostname -and $self.HostName) { $Hostname = [string]$self.HostName }
            if (-not $ipv4 -and $self.TailscaleIPs) {
                foreach ($ip in $self.TailscaleIPs) {
                    if ($ip -notmatch ":") { $ipv4 = $ip; break }
                }
            }
            if (-not $ipv6 -and $self.TailscaleIPs) {
                foreach ($ip in $self.TailscaleIPs) {
                    if ($ip -match ":") { $ipv6 = $ip; break }
                }
            }
        }
    } elseif ($rj.Code -ne 0) {
        $backend = "error:$($rj.Out)"
    }
    if (-not $Hostname) { $Hostname = $env:COMPUTERNAME }
    return [ordered]@{
        hostname = $Hostname
        ipv4 = $ipv4
        ipv6 = $ipv6
        dns_name = $dns
        backend = $backend
        online = $online
        key_expiry = $keyExp
        health = $health
        collected_at = (Get-Date).ToUniversalTime().ToString("o")
    }
}

function Get-Changes($prev, $cur, [double]$KeyWarnDays) {
    if (-not $prev) { return @("initial") }
    $changes = @()
    foreach ($pair in @(
        @("ipv4","IPv4"), @("ipv6","IPv6"), @("backend","Backend"),
        @("online","Online"), @("dns_name","DNS"), @("key_expiry","KeyExpiry")
    )) {
        $k = $pair[0]; $label = $pair[1]
        if ("$($prev.$k)" -ne "$($cur.$k)") { $changes += $label }
    }
    $ph = ($prev.health | ConvertTo-Json -Compress)
    $ch = ($cur.health | ConvertTo-Json -Compress)
    if ($ph -ne $ch) { $changes += "Health" }
    if ($cur.key_expiry) {
        try {
            $exp = [DateTime]::Parse($cur.key_expiry).ToUniversalTime()
            $days = ($exp - [DateTime]::UtcNow).TotalDays
            if ($days -le $KeyWarnDays) {
                $prevDays = $null
                if ($prev.key_expiry) {
                    $prevDays = ([DateTime]::Parse($prev.key_expiry).ToUniversalTime() - [DateTime]::UtcNow).TotalDays
                }
                if (($null -eq $prevDays) -or ($prevDays -gt $KeyWarnDays) -or ($changes -contains "KeyExpiry")) {
                    $changes += "KeyWarn"
                }
            }
        } catch {}
    }
    return $changes
}

function Send-Telegram([string]$Token, $ChatIds, [string]$Text) {
    $ok = 0
    foreach ($id in $ChatIds) {
        $uri = "https://api.telegram.org/bot$Token/sendMessage"
        try {
            Invoke-RestMethod -Method Post -Uri $uri -Body @{ chat_id = "$id"; text = $Text } | Out-Null
            $ok++
        } catch {
            Write-Host "telegram $id failed: $_"
        }
    }
    return $ok
}

function Format-Message($snap, $changes) {
    $lines = @(
        "Tailscale Status",
        "Maschine: $($snap.hostname)"
    )
    if ($snap.dns_name) { $lines += "DNS: $($snap.dns_name)" }
    $lines += "IPv4: $(if ($snap.ipv4) { $snap.ipv4 } else { '—' })"
    $lines += "IPv6: $(if ($snap.ipv6) { $snap.ipv6 } else { '—' })"
    $lines += "Backend: $($snap.backend)"
    if ($null -ne $snap.online) { $lines += "Online: $($snap.online)" }
    if ($snap.key_expiry) { $lines += "KeyExpiry: $($snap.key_expiry)" }
    if ($snap.health -and $snap.health.Count -gt 0) {
        $lines += "Health: $($snap.health -join ', ')"
    }
    $lines += "Änderung: $($changes -join ', ')"
    $lines += "Zeit: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')"
    return ($lines -join "`n")
}

$cfg = Get-Config
$interval = 60
if ($cfg.interval_sec) { $interval = [int]$cfg.interval_sec }
$warnDays = 14
if ($cfg.key_warn_days) { $warnDays = [double]$cfg.key_warn_days }
$hostName = ""
if ($cfg.hostname_override) { $hostName = [string]$cfg.hostname_override }

function Invoke-Cycle([switch]$ForceSend) {
    $snap = Get-Snapshot $hostName
    $prev = $null
    if (Test-Path $StatePath) {
        $prev = Get-Content -Raw $StatePath | ConvertFrom-Json
    }
    $changes = @(Get-Changes $prev $snap $warnDays)
    if ($ForceSend -and $changes.Count -eq 0) { $changes = @("manual") }
    if ($changes.Count -eq 0) { return }
    $msg = Format-Message $snap $changes
    $sent = Send-Telegram $cfg.telegram_token $cfg.chat_ids $msg
    ($snap | ConvertTo-Json -Depth 5) | Set-Content -Path $StatePath -Encoding UTF8
    Write-Host "$(Get-Date -Format o) change=$($changes -join ',') sent=$sent"
}

if ($Once -or $Force) {
    Invoke-Cycle -ForceSend:$Force
    exit 0
}

Write-Host "tailscale_notify running every ${interval}s"
while ($true) {
    try { Invoke-Cycle } catch { Write-Host "cycle error: $_" }
    Start-Sleep -Seconds ([Math]::Max(15, $interval))
}
