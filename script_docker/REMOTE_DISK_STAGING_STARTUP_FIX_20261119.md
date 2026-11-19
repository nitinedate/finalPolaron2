# Polaron Docker startup: missing remote-disk-staging bind (2026-11-19)

## Observed error

`STARTUP ERROR: Missing bind-mounted deployment file/directory for api: D:\project1\polaron\finalPolaron2\backup\forensic-data\remote-disk-staging (container path /remote-disk-staging).`

This is a local Windows Docker Compose host-path validation error, not an HTTPS, registry, or GPU failure.

## One-time recovery on the current installation

Open PowerShell:

```powershell
cd D:\project1\polaron\finalPolaron2\script_docker
.\Repair-RemoteDiskStaging.ps1
.\start_docker_prod.cmd
```

If you have not installed the corrected project yet, create the shown directory manually:

```powershell
New-Item -ItemType Directory -Force -Path 'D:\project1\polaron\finalPolaron2\backup\forensic-data\remote-disk-staging'
```

**Important:** If remote disk evidence was previously staged, restore the original staging folder, and confirm the `.env` `AETHERIS_BACKUP_DIR` points to your existing backup storage. Do not delete, reset, or prune evidence and database volumes.

## Permanent code correction

`script_docker/start_docker.lib.ps1` now explicitly treats `/remote-disk-staging` as a runtime directory. Normal startup automatically creates it when missing; `-ValidateOnly` checks without creating it. Existing contents are preserved. The same allowlist still rejects missing source-code/configuration binds, so deployment errors cannot be bypassed wholesale.

`tests/windows_docker_file_checks.ps1` verifies dry-run behavior, directory creation, preservation of existing files, and wrong-type rejection. Execute on Windows with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\windows_docker_file_checks.ps1
```

The helper `script_docker/Repair-RemoteDiskStaging.ps1` handles relative `AETHERIS_BACKUP_DIR` paths and refuses to replace an absent absolute configured backup root.

## Validation limitations

The source has been statically validated and ZIP integrity checked in the packaging environment. Actual Windows PowerShell execution and live Docker startup must be run on the target PC.
