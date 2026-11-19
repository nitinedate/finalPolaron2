@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Polaron_Fix_RemoteStaging.ps1" -ProjectRoot "D:\project1\polaron\finalPolaron2"
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
  echo Repair failed; read the specific error above.
) else (
  echo The repair succeeded. You can restart Polaron now.
)
pause
exit /b %EXITCODE%
