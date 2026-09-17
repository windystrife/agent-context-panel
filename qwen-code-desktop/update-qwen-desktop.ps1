# Update Qwen Code Desktop when the in-app "Install and restart" fails.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File update-qwen-desktop.ps1             # update if newer
#   ... -CheckOnly                          # report installed vs latest, change nothing
#   ... -Force                              # reinstall the latest even if already installed
#   ... -InstallDir D:\apps\qwen-desktop    # a copy that was not installed to the default folder
#   ... -Launcher D:\apps\launch-qwen.ps1   # start the app through this script
#   ... -NoPatch / -NoLaunch
#   ... -RemoveLegacyElectron               # let the installer uninstall the old Electron app (0.0.x)
#
# Why the in-app update fails: the app exits to run its NSIS installer, but the daemon it
# spawned (runtime\qwen-code\node\node.exe ... serve) and that daemon's ACP child keep
# running and hold node.exe open, so the installer cannot overwrite it and stops early.
# Unless HKCU\Software\alibaba\Qwen Code Desktop names another folder, the installer targets
# %LOCALAPPDATA%\Qwen Code Desktop, so a copy unpacked elsewhere (e.g. D:\apps\qwen-desktop) is
# never updated from inside the app. And when the old Electron "Qwen Code Desktop" 0.0.x is
# still installed, the installer first runs its uninstaller and aborts (exit code 2) if
# that fails - before copying a single file.
#
# What this does: reads the official update feed, finds or downloads the installer and
# checks its minisign signature with the key embedded in the app (the same check the
# app's updater does), stops every process started from the install folder, installs
# silently into -InstallDir, verifies every runtime file against checksums.json,
# re-applies the local UI patches (inject.py, ctxpop.py) and starts the app again.
#
# ASCII only on purpose: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI.

[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Qwen Code Desktop'),
    [switch]$Force,
    [switch]$CheckOnly,
    [switch]$NoPatch,
    [switch]$NoLaunch,
    [string]$Launcher,
    [string]$Python,
    [string]$PatchDir,  # default: this script's folder
    [string]$Installer,
    [switch]$RemoveLegacyElectron
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$AppName  = 'Qwen Code Desktop'
$InstallDir = $InstallDir.TrimEnd('\')
$AppExe   = Join-Path $InstallDir 'qwen-code-desktop.exe'
$Runtime  = Join-Path $InstallDir 'runtime\qwen-code'
$NodeExe  = Join-Path $Runtime 'node\node.exe'
$WebShell = Join-Path $Runtime 'lib\web-shell'
$Helper   = Join-Path $PSScriptRoot 'update-helper.mjs'
if (-not $PatchDir) { $PatchDir = $PSScriptRoot }  # $PSScriptRoot is still empty inside param() on PowerShell 5.1
$WorkDir  = Join-Path $env:LOCALAPPDATA 'qwen-desktop-update'
$RunLog   = Join-Path $env:LOCALAPPDATA 'com.alibaba.qwen-code\logs\desktop-runtime.log'
$Feeds = @(
    'https://qwen-code-assets.oss-cn-hangzhou.aliyuncs.com/desktop/latest/desktop-latest.json',
    'https://github.com/QwenLM/qwen-code/releases/download/desktop-latest/desktop-latest.json'
)
# The minisign key the app verifies its updates with. Read from the installed exe when
# possible; this copy (from 0.24.0) is only the fallback for a missing or broken install.
$PinnedKey = 'dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDkzMTlFNDMyRTVFNkUyNUMKUldSYzR1YmxNdVFaa3pJWjBPMERVVUw0akFvU3ZxS0pNMTFrcUhCbDlUaEVuRkVEVEcxaDBHNEcK'

function Say([string]$Message) { Write-Host "[qwen-update] $Message" }
function Fail([string]$Message) { Write-Host "[qwen-update] FAILED: $Message" -ForegroundColor Red; exit 1 }

function ConvertTo-Version([string]$Text) {
    if (-not $Text) { return [version]'0.0.0' }
    try { return [version](($Text -split '[-+ ]')[0]) } catch { return [version]'0.0.0' }
}

# Run a console program, echo its output, return its exit code. stderr is folded into
# stdout with ErrorActionPreference relaxed so PowerShell 5.1 does not turn it into errors.
function Invoke-Native([string]$Exe, [string[]]$ArgList) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Exe @ArgList 2>&1 | ForEach-Object { Say "  $_" } } finally { $ErrorActionPreference = $old }
    return $LASTEXITCODE
}

function Get-NodeForHelper {
    $n = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($n) { return $n.Source }
    if (-not (Test-Path -LiteralPath $NodeExe)) { Fail 'no node.exe on PATH and none in the install folder (needed for signature checks)' }
    # run a copy, so our own checks never hold the install folder's node.exe open
    $copy = Join-Path $WorkDir 'node.exe'
    Copy-Item -LiteralPath $NodeExe -Destination $copy -Force
    return $copy
}

function Get-AppProcesses {
    $root = $InstallDir + '\'
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
    })
}

function Test-Writable([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    try { $f = [IO.File]::Open($Path, 'Open', 'ReadWrite', 'None'); $f.Close(); return $true } catch { return $false }
}

function Get-Feed {
    foreach ($u in $Feeds) {
        try {
            $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 30
            $text = $r.Content
            if ($text -is [byte[]]) { $text = [Text.Encoding]::UTF8.GetString($text) }
            $j = $text | ConvertFrom-Json
            if ($j.version -and $j.platforms.'windows-x86_64'.url) { Say "feed: $u"; return $j }
            Say "feed has no windows-x86_64 entry: $u"
        } catch { Say "feed unreachable: $u ($($_.Exception.Message))" }
    }
    Fail 'could not read the update feed'
}

function Get-AppKey {
    if (Test-Path -LiteralPath $AppExe) {
        $s = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($AppExe))
        $m = [regex]::Matches($s, 'dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6[A-Za-z0-9+/=]+')
        if ($m.Count -eq 1) {
            if ($m[0].Value -ne $PinnedKey) { Say 'note: the installed app carries a different update key than the pinned one; using the app''s key' }
            return $m[0].Value
        }
    }
    return $PinnedKey
}

# The old Electron app's uninstaller, if that app is still installed for this user (the key
# and file name the 0.24 installer's pre-install hook looks for).
function Get-LegacyUninstaller {
    $k = 'HKCU:\Software\821b18a9-7c63-5bb4-9e20-51ba63d5ecc3'
    $loc = (Get-ItemProperty -LiteralPath $k -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
    if (-not $loc) { return $null }
    $u = Join-Path $loc 'Uninstall Qwen Code Desktop.exe'
    if (Test-Path -LiteralPath $u) { return $u }
    return $null
}

function Get-ShortcutSnapshot {
    $sh = New-Object -ComObject WScript.Shell
    $dirs = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
    @(Get-ChildItem -LiteralPath $dirs -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'qwen' } |
        ForEach-Object { $l = $sh.CreateShortcut($_.FullName); ('{0} -> {1} {2}' -f $_.FullName, $l.TargetPath, $l.Arguments).TrimEnd() })
}

# Re-apply the local UI patches (both patchers are idempotent). When every runtime file is
# pristine, backups they made earlier belong to an older version: they would refuse to take
# a fresh one, and --revert would put old-version files into this version. Move them away.
function Invoke-Patches([bool]$Pristine) {
    if ($Pristine) {
        $stale = @(Get-ChildItem -LiteralPath $WebShell -Recurse -File -Filter '*.bak-before-*' -ErrorAction SilentlyContinue)
        if ($stale) {
            $dest = Join-Path $WorkDir ('stale-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
            New-Item -ItemType Directory -Force -Path $dest | Out-Null
            foreach ($f in $stale) { Move-Item -LiteralPath $f.FullName -Destination $dest -Force; Say "moved old patch backup $($f.Name) -> $dest" }
        }
    }
    if ($NoPatch) { return }
    $py = @()
    if ($Python) { $py = @($Python) }
    elseif (Get-Command py.exe -ErrorAction SilentlyContinue) { $py = @((Get-Command py.exe).Source, '-3') }
    elseif (Get-Command python.exe -ErrorAction SilentlyContinue) { $py = @((Get-Command python.exe).Source) }
    $patches = @(
        @{ Script = 'inject.py'; Args = @('--html', (Join-Path $WebShell 'index.html')) },
        @{ Script = 'ctxpop.py'; Args = @('--web-shell', $WebShell) }
    )
    foreach ($p in $patches) {
        $script = Join-Path $PatchDir $p.Script
        if (-not (Test-Path -LiteralPath $script)) { continue }
        if (-not $py) { Say "skip $($p.Script): no python found (pass -Python)"; continue }
        Say "patch: $($p.Script)"
        $pyArgs = @($py | Select-Object -Skip 1) + @($script) + $p.Args
        if ((Invoke-Native $py[0] $pyArgs) -ne 0) { Say "WARNING: $($p.Script) does not fit $latest - the app works, just without that patch" }
    }
}

# ---------------------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
if (-not (Test-Path -LiteralPath $Helper)) { Fail "missing $Helper" }
$Node = Get-NodeForHelper

$installed = $null
if (Test-Path -LiteralPath $AppExe) { $installed = (Get-Item -LiteralPath $AppExe).VersionInfo.ProductVersion }
$manifestVersion = $null
$manifestPath = Join-Path $Runtime 'manifest.json'
if (Test-Path -LiteralPath $manifestPath) { $manifestVersion = (Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).desktopVersion }
Say "install folder : $InstallDir"
Say "installed      : app $installed, runtime $manifestVersion"

$feed = Get-Feed
$latest = $feed.version
$win = $feed.platforms.'windows-x86_64'
Say "latest         : $latest ($($feed.pub_date))"

$runtimeRc = -1
if (Test-Path -LiteralPath (Join-Path $Runtime 'checksums.json')) {
    Say 'checking runtime files against checksums.json ...'
    $runtimeRc = Invoke-Native $Node @($Helper, 'verify-runtime', $Runtime)
}

$procs = @(Get-AppProcesses)
$ui = @($procs | Where-Object { $_.Name -ieq 'qwen-code-desktop.exe' })
$orphans = @()
if (-not $ui) { $orphans = @($procs) }
foreach ($p in $procs) { Say ("running        : pid {0} {1} started {2}" -f $p.ProcessId, $p.Name, $p.CreationDate) }
if ($orphans) { Say "$($orphans.Count) process(es) from the install folder are running without the app window - left over from a failed update; they lock node.exe" }

$reasons = @()
if ($Force) { $reasons += '-Force' }
if (-not $installed) { $reasons += 'not installed' }
elseif ((ConvertTo-Version $latest) -gt (ConvertTo-Version $installed)) { $reasons += "newer version $latest" }
if ($installed -and $manifestVersion -ne $installed) { $reasons += 'app and runtime versions differ (half-finished update)' }
if ($runtimeRc -eq 1) { $reasons += 'runtime files damaged or incomplete' }

if ($CheckOnly) {
    if ($reasons) { Say "update needed: $($reasons -join ', ')" } else { Say 'up to date' }
    exit 0
}

if (-not $reasons) {
    Say "up to date ($installed)"
    Invoke-Patches ($runtimeRc -eq 0)
    if ($orphans) {
        foreach ($p in $orphans) { Say "stopping leftover pid $($p.ProcessId) $($p.Name)"; Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    exit 0
}
Say "installing because: $($reasons -join ', ')"

# --- a verified installer ---------------------------------------------------------------
$key = Get-AppKey
$candidates = @()
if ($Installer) { $candidates += $Installer }
$candidates += @(Get-ChildItem -LiteralPath $env:TEMP -Directory -Filter "$AppName-*-updater-*" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -Filter '*.exe' -File } |
    ForEach-Object { $_.FullName })
$download = Join-Path $WorkDir ("Qwen-Code-Desktop_{0}_x64-setup.exe" -f $latest)
if (Test-Path -LiteralPath $download) { $candidates += $download }

$setup = $null
foreach ($c in $candidates) {
    Say "checking signature of $c"
    if ((Invoke-Native $Node @($Helper, 'verify-sig', $c, $win.signature, $key)) -eq 0) { $setup = $c; break }
}
if (-not $setup) {
    Say "downloading $($win.url)"
    $part = "$download.part"
    $rc = Invoke-Native 'curl.exe' @('-sS', '-fL', '--retry', '3', '-o', $part, $win.url)
    if ($rc -ne 0) { Fail "download failed (curl exit $rc)" }
    if ((Invoke-Native $Node @($Helper, 'verify-sig', $part, $win.signature, $key)) -ne 0) {
        Remove-Item -LiteralPath $part -Force
        Fail 'downloaded installer does not match the signature in the feed - not installing it'
    }
    Move-Item -LiteralPath $part -Destination $download -Force
    $setup = $download
}
Say "installer      : $setup"

# --- stop everything that runs from the install folder ----------------------------------
$shortcutsBefore = Get-ShortcutSnapshot
foreach ($p in @(Get-Process -Name 'qwen-code-desktop' -ErrorAction SilentlyContinue)) {
    if ($p.Path -and $p.Path.StartsWith($InstallDir + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Say "closing the app window (pid $($p.Id))"
        [void]$p.CloseMainWindow()
    }
}
for ($i = 0; $i -lt 20; $i++) {
    $left = @(Get-AppProcesses)
    if (-not $left) { break }
    if ($i -ge 6) {
        foreach ($p in $left) { Say "stopping pid $($p.ProcessId) $($p.Name)"; Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Milliseconds 500
}
$locked = @($NodeExe, $AppExe | Where-Object { -not (Test-Writable $_) })
for ($i = 0; $locked -and $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    $locked = @($NodeExe, $AppExe | Where-Object { -not (Test-Writable $_) })
}
if ($locked) {
    Get-AppProcesses | ForEach-Object { Say "still running: pid $($_.ProcessId) $($_.ExecutablePath)" }
    Fail "still locked: $($locked -join ', ') (a process from another user or an elevated one? run this as administrator)"
}
Say 'install folder is unlocked'

# --- install ----------------------------------------------------------------------------
$started = Get-Date
Say "running installer silently into $InstallDir ..."
# /D= must be last and unquoted (NSIS rule); /NS = do not create shortcuts
# Keep the old Electron app out of it: park its uninstaller so the installer's hook skips it
# (the same state as a machine where that app is already gone), put it back right after.
$legacy = $null
$parked = $null
if (-not $RemoveLegacyElectron) { $legacy = Get-LegacyUninstaller }
if ($legacy) {
    $parked = "$legacy.qwen-update-hold"
    try {
        Rename-Item -LiteralPath $legacy -NewName (Split-Path $parked -Leaf)
        Say "old Electron Qwen Code Desktop is installed; its uninstaller is parked during the install so that app is left alone"
    } catch {
        $parked = $null
        Say "WARNING: could not park $legacy ($($_.Exception.Message)); the installer may abort"
    }
}
$exitCode = $null
try {
    $proc = Start-Process -FilePath $setup -ArgumentList @('/S', '/NS', "/D=$InstallDir") -PassThru
    $null = $proc.Handle
    if ($proc.WaitForExit(15 * 60 * 1000)) { $exitCode = $proc.ExitCode }
} finally {
    if ($parked -and (Test-Path -LiteralPath $parked)) {
        Rename-Item -LiteralPath $parked -NewName (Split-Path $legacy -Leaf)
        Say "put the old Electron uninstaller back ($legacy)"
    }
}
if ($null -eq $exitCode) { Fail 'installer did not finish within 15 minutes' }
Say "installer exit code $exitCode after $([int]((Get-Date) - $started).TotalSeconds) s"
if ($exitCode -eq 2) { Fail 'installer aborted itself (exit code 2); known cause: it could not uninstall the old Electron Qwen Code Desktop (run without -RemoveLegacyElectron, or close that app)' }
if ($exitCode -ne 0) { Fail "installer exit code $exitCode" }

# --- verify -----------------------------------------------------------------------------
$nowApp = (Get-Item -LiteralPath $AppExe).VersionInfo.ProductVersion
$nowRuntime = (Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).desktopVersion
Say "now installed  : app $nowApp, runtime $nowRuntime"
if ($nowApp -ne $latest -or $nowRuntime -ne $latest) { Fail "expected $latest after install" }
$uninst = Join-Path $InstallDir 'uninstall.exe'
if (-not (Test-Path -LiteralPath $uninst) -or (Get-Item -LiteralPath $uninst).LastWriteTime -lt $started.AddSeconds(-2)) {
    Fail 'the installer did not reach its last step (uninstall.exe was not rewritten)'
}
Say 'checking runtime files against checksums.json ...'
if ((Invoke-Native $Node @($Helper, 'verify-runtime', $Runtime)) -ne 0) { Fail 'runtime files do not match checksums.json after install' }

$shortcutsAfter = Get-ShortcutSnapshot
$gone = @($shortcutsBefore | Where-Object { $shortcutsAfter -notcontains $_ })
$new = @($shortcutsAfter | Where-Object { $shortcutsBefore -notcontains $_ })
foreach ($s in $gone) { Say "shortcut was: $s" }
foreach ($s in $new) { Say "shortcut now: $s" }
if (-not ($gone -or $new)) { Say "shortcuts unchanged ($($shortcutsAfter.Count) with 'qwen' in the name)" }

Invoke-Patches $true

# --- start the app ----------------------------------------------------------------------
if ($NoLaunch) { Say "done: $latest installed (not started, -NoLaunch)"; exit 0 }
if ($env:SSH_CONNECTION) { Say "done: $latest installed (not started: this is an SSH session, start it from the desktop)"; exit 0 }
# only look at log text written after the launch (the app may append or start a new file)
$logFrom = 0
if (Test-Path -LiteralPath $RunLog) { $logFrom = (Get-Item -LiteralPath $RunLog).Length }
if ($Launcher) {
    Say "starting the app via $Launcher"
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', "`"$Launcher`"") -WindowStyle Hidden
} else {
    # through explorer, so the app is not a child of this console
    Say 'starting the app'
    Start-Process -FilePath 'explorer.exe' -ArgumentList "`"$AppExe`""
}
$port = $null
for ($i = 0; $i -lt 90 -and -not $port; $i++) {
    Start-Sleep -Seconds 1
    if (-not (Test-Path -LiteralPath $RunLog)) { continue }
    try {
        $fs = [IO.File]::Open($RunLog, 'Open', 'Read', 'ReadWrite, Delete')
        try {
            if ($fs.Length -lt $logFrom) { $logFrom = 0 }
            [void]$fs.Seek($logFrom, 'Begin')
            $text = (New-Object IO.StreamReader($fs)).ReadToEnd()
        } finally { $fs.Close() }
        $m = [regex]::Match($text, 'qwen serve listening on http://127\.0\.0\.1:(\d+)')
        if ($m.Success) { $port = $m.Groups[1].Value }
    } catch { }
}
if (-not $port) { Fail "installed $latest, but the app's daemon did not report listening within 90 s (see $RunLog)" }
Say "done: $latest installed and running (daemon on 127.0.0.1:$port)"
exit 0
