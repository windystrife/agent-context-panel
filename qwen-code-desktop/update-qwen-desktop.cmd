@echo off
rem Double-click to update Qwen Code Desktop (options: -CheckOnly, -Force, -InstallDir <dir>, -NoPatch, -NoLaunch).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-qwen-desktop.ps1" %*
set "RC=%ERRORLEVEL%"
if not defined QWEN_UPDATE_NOPAUSE pause
exit /b %RC%
