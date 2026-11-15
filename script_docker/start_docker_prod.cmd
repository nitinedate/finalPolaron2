@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Polaron - Production Docker HTTPS (shared infrastructure)
set "ROOT=%~dp0.."
set "LAUNCHER=%~dp0start_docker.ps1"
cd /d "%ROOT%" || goto :badroot

echo.
echo ==============================================================
echo  POLARON FORENSIC SUITE - PRODUCTION HTTPS
echo ==============================================================
echo  HTTPS    : https://PUBLIC_HTTPS_IP/ and, when set, https://PUBLIC_HTTPS_DOMAIN/
echo             Both come from the root .env. An empty domain means IP only.
echo  Certs    : trusted Let's Encrypt certificates, renewed automatically.
echo  Common   : PostgreSQL, Redis, MinIO, OpenSearch, pgAdmin,
echo             MailHog - shared with other projects, not stopped.
echo  Neo4j    : Polaron-owned Docker service.
echo  AI       : host Ollama and auto-provisioned host SuryaOCR; no Docker Ollama.
echo  Models   : NotMythos forensics; The-Xploiter on-demand review.
echo  Evidence : no Docker volume prune; keep existing case volumes.
echo  Build    : check cached packages, stop Polaron apps, compile, restart.
echo  Cache    : reuse CUDA, Python, pip, npm and Docker image layers.
echo  Legacy  : one-time index handover requires -HandoverLegacyOpenSearch.
echo  Required : production TLS needs public TCP 80 and 443 forwarded to this PC.
echo.
if not exist "%LAUNCHER%" goto :missing
if not exist "%ROOT%\services\common\docker-compose.yml" goto :missingcommon
where docker.exe >nul 2>nul
if errorlevel 1 goto :missingdocker
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LAUNCHER%" -Profile prod %*
set "EXITCODE=%ERRORLEVEL%"
goto :finish
:badroot
echo ERROR: Could not enter the Polaron project root.
set "EXITCODE=2"
goto :finish
:missing
echo ERROR: Missing script_docker\start_docker.ps1.
echo Extract the launcher update into the root of the complete project.
set "EXITCODE=2"
goto :finish
:missingcommon
echo ERROR: Missing services\common\docker-compose.yml.
echo Polaron starts shared services through services\common, not common-docker.
echo Restore the services\common folder from the startup patch or complete project.
set "EXITCODE=2"
goto :finish
:missingdocker
echo ERROR: Docker CLI was not found in PATH. Install/start Docker Desktop.
set "EXITCODE=2"
:finish
echo.
if not "%EXITCODE%"=="0" (
  echo Startup failed with exit code %EXITCODE%.
) else (
  echo Production Docker startup finished. Open the READY HTTPS address shown above.
)
if not "%AETHERIS_NO_PAUSE%"=="1" pause
exit /b %EXITCODE%
