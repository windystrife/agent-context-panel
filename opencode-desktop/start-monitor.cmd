@echo off
REM ===========================================================
REM  OpenCode Desktop - conversation context monitor
REM  Dashboard: http://127.0.0.1:8096
REM
REM  Reads (read-only, on a copy):
REM    %%USERPROFILE%%\.local\share\opencode\opencode.db
REM    %%USERPROFILE%%\.cache\opencode\models.json   (context limits + prices)
REM
REM  Port 8096, not 8097: Qwen Code Desktop's renderer probes
REM  localhost:8097 for React DevTools.
REM  Set WSL_DISTRO to pick a distro; default is your default one.
REM ===========================================================
setlocal
if defined WSL_DISTRO (set "D=-d %WSL_DISTRO%") else (set "D=")
start "" http://127.0.0.1:8096
wsl.exe %D% -e bash -c "cd \"$(wslpath -a '%~dp0')\" && python3 monitor.py --port 8096"
