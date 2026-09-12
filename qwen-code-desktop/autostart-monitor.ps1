# Keeps the Qwen Code Desktop context monitor running for this Windows session.
# Launched hidden from the Startup folder by install-autostart.ps1.
#
# Why a loop rather than a one-shot start: the monitor runs inside WSL, and WSL
# can be shut down underneath it mid-session. That has happened: the monitor
# printed its banner, then got a clean SIGTERM with no error. A one-shot start
# would leave the in-app panel on "monitor off" until the next logon; this
# brings it back within about 10 seconds instead.
#
# ASCII only on purpose: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI.

$ErrorActionPreference = 'Continue'
$distro  = 'Ubuntu-24.04'
$workdir = '/mnt/h/Claude/agent-context-panel/qwen-code-desktop'
$port    = 8098
$log     = Join-Path $env:LOCALAPPDATA 'qwen-ctx-monitor.log'

function Write-Log([string]$msg) {
    try { Add-Content -LiteralPath $log -Value ("{0}  {1}" -f (Get-Date -Format s), $msg) } catch { }
}

function Test-MonitorUp {
    try {
        $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/api/stats" -f $port) -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

Write-Log ("launcher started, pid {0}" -f $PID)
while ($true) {
    if (Test-MonitorUp) {
        # Already served, e.g. by a manual run. Do not start a second copy that
        # would only die on "address already in use"; just look again later.
        Start-Sleep -Seconds 30
        continue
    }
    Write-Log "starting monitor"
    wsl.exe -d $distro -e bash -c ("cd {0} && exec python3 monitor.py --port {1}" -f $workdir, $port) *> $null
    Write-Log ("monitor exited with code {0}; restarting in 10 s" -f $LASTEXITCODE)
    Start-Sleep -Seconds 10
}
