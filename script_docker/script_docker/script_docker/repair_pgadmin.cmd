@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Polaron - Repair Shared pgAdmin Login Email
set "ROOT=%~dp0.."
cd /d "%ROOT%" || exit /b 2
echo Repairing pgAdmin startup configuration without deleting volumes...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\ensure_pgadmin_email.ps1" -ProjectRoot "%ROOT%"
if errorlevel 1 (
  echo ERROR: pgAdmin email configuration validation failed.
  exit /b 1
)
where docker.exe >nul 2>nul
if errorlevel 1 (echo ERROR: Docker CLI was not found. & exit /b 2)
docker compose --project-directory "%ROOT%" -p aetheris-common -f "%ROOT%\services\common\docker-compose.yml" up -d --no-deps --no-build --pull missing --force-recreate pgadmin
if errorlevel 1 (echo ERROR: Cannot recreate pgAdmin container. & exit /b 1)
echo.
echo pgAdmin restart requested. Check logs and sign in using your existing account email.
docker compose --project-directory "%ROOT%" -p aetheris-common -f "%ROOT%\services\common\docker-compose.yml" logs --tail 35 pgadmin
exit /b %ERRORLEVEL%
