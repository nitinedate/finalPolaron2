@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Repair-RemoteDiskStaging.ps1"
exit /b %ERRORLEVEL%
