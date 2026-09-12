@echo off
REM ===========================================================
REM  Qwen Code Desktop - conversation context monitor
REM  Dashboard: http://127.0.0.1:8098
REM
REM  Reads (read-only) the records the app already writes:
REM    %USERPROFILE%\.qwen\usage\token-usage-YYYY-MM.jsonl
REM    %USERPROFILE%\.qwen\usage_record.jsonl
REM    %USERPROFILE%\.qwen\settings.json          (context windows)
REM    %USERPROFILE%\.craft-agent\workspaces\...  (session status)
REM    %USERPROFILE%\.dscode\models.json          (price table)
REM
REM  Runs under WSL because Windows python here is the Store stub.
REM  The in-app panel (inject.py) also talks to this server.
REM ===========================================================
start "" http://127.0.0.1:8098
wsl.exe -d Ubuntu-24.04 -e bash -c "cd /mnt/h/Claude/agent-context-panel/qwen-code-desktop && python3 monitor.py --port 8098"
