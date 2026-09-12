@echo off
REM ===========================================================
REM  Qwen Code Desktop - conversation context monitor
REM  Dashboard: http://127.0.0.1:8098
REM
REM  Reads (read-only) records the app already writes:
REM    %%USERPROFILE%%\.qwen\usage\token-usage-YYYY-MM.jsonl
REM    %%USERPROFILE%%\.qwen\usage_record.jsonl
REM    %%USERPROFILE%%\.qwen\settings.json          (context windows)
REM    %%USERPROFILE%%\.craft-agent\workspaces\...  (session status)
REM    %%USERPROFILE%%\.dscode\models.json          (price table, optional)
REM
REM  Runs under WSL because a Windows "python" is often the Store stub.
REM  The in-app panel (inject.py) reads this server too.
REM  Set WSL_DISTRO to pick a distro; default is your default one.
REM ===========================================================
setlocal
if defined WSL_DISTRO (set "D=-d %WSL_DISTRO%") else (set "D=")
start "" http://127.0.0.1:8098
wsl.exe %D% -e bash -c "cd \"$(wslpath -a '%~dp0')\" && python3 monitor.py --port 8098"
