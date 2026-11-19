Polaron remote-disk-staging startup repair - 19 Nov 2026

Symptom:
  Missing bind-mounted deployment file/directory for api:
  D:\project1\polaron\finalPolaron2\backup\forensic-data\remote-disk-staging
  (container path /remote-disk-staging)

Cause confirmed from source:
  Old script_docker\start_docker.lib.ps1 runtimeTargets allowlist lacks
  '/remote-disk-staging'. Installing just the frontend fix does not update this file.

Action:
  Extract both Polaron_Fix_RemoteStaging.ps1 and .cmd together to Downloads.
  Double-click the .cmd OR run this PowerShell command:

  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Polaron_Fix_RemoteStaging.ps1" -ProjectRoot "D:\project1\polaron\finalPolaron2"

  The tool patches ONLY the active runtime allowlist after taking a dated backup.
  It creates the staging folder if missing, preserves existing files, and does
  not delete any data, change .env, stop Docker, or reset the database.

  If your project lives elsewhere, pass its path through -ProjectRoot.

Verify:
  Select-String -Path 'D:\project1\polaron\finalPolaron2\script_docker\start_docker.lib.ps1' -Pattern '/remote-disk-staging'
  Test-Path 'D:\project1\polaron\finalPolaron2\backup\forensic-data\remote-disk-staging' -PathType Container

Start:
  & 'D:\project1\polaron\finalPolaron2\script_docker\start_docker_prod.cmd'

If a custom absolute AETHERIS_BACKUP_DIR points to disconnected storage,
reconnect/restore the original backup drive rather than creating an empty
replacement. Do not use docker compose down -v or docker volume prune.
