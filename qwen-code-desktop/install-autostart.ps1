# Install or remove autostart for the Qwen Code Desktop context monitor.
#
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1              # install
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Uninstall   # remove
#
# Uses a per-user Startup folder shortcut: no admin rights, no scheduled task,
# no registry edits, and removing it is deleting one file.
#
# ASCII only on purpose: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI.

param([switch]$Uninstall)

$startup = [Environment]::GetFolderPath('Startup')
$lnk     = Join-Path $startup 'Qwen Context Monitor.lnk'
$script  = Join-Path $PSScriptRoot 'autostart-monitor.ps1'

if ($Uninstall) {
    if (Test-Path -LiteralPath $lnk) {
        Remove-Item -LiteralPath $lnk -Force
        "removed $lnk"
        "a launcher that is already running keeps going until logoff; to stop it now:"
        "  Get-CimInstance Win32_Process -Filter ""Name='powershell.exe'"" | Where-Object { `$_.CommandLine -like '*autostart-monitor.ps1*' } | ForEach-Object { Stop-Process -Id `$_.ProcessId }"
    } else {
        "not installed"
    }
    return
}

if (-not (Test-Path -LiteralPath $script)) { throw "missing $script" }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath       = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$sc.Arguments        = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $script
$sc.WorkingDirectory = $PSScriptRoot
$sc.WindowStyle      = 7          # minimized, in case a host ignores -WindowStyle Hidden
$sc.Description      = 'Keeps the Qwen Code Desktop context monitor running (agent-context-panel)'
$sc.Save()

"installed $lnk"
"  target : $($sc.TargetPath)"
"  args   : $($sc.Arguments)"
