# Polaron inventory stage: Errno 5 temporary workspace recovery

## Fault
`pathlib.Path.is_file()` and `TemporaryDirectory.cleanup()` both raised `OSError: [Errno 5] Input/output error` for `/app/data/tmp/aetheris_stage_*/result.json` during the `inventory` stage.

## Code update
- `backend/app/services/forensic_stage_worker.py` now creates small stage handshake/result files in `/tmp/aetheris-stage-control`, *not* the host-backed `/app/data/tmp` selected by `TMPDIR`.
- Parent checks storage free space, raises retryable `StageWaiting` on EIO, ENODEV, ESTALE, EROFS and ENOSPC; missing or incomplete child output stays retryable.
- Child writes the result to a new temporary file, flushes/fsyncs, then commits using atomic `os.replace`; failed writes cannot create a misleading complete result.
- Temporary cleanup logs storage faults without overriding the original child result or failure.
- Other bulk extraction and forensic evidence storage paths are unchanged; source images and completed MinIO parts are untouched.

## Before deployment
Wait for currently active extraction uploads to complete; stop/allow inventory worker tasks to reach a safe checkpoint before replacing files. Back up your application code. Extract the patch at `D:\project1\polaran2` preserving directories, then rebuild the stage-executing worker images with the existing `script_docker\start_docker_local.cmd` workflow. A container recreate *without rebuilding* will not copy the new Python code into baked images.

## Diagnose the underlying host storage
PowerShell:
```powershell
# Identify the job data mount and its host drive:
docker inspect aetheris-forensic-worker-parse-1 --format '{{range .Mounts}}{{println .Source " -> " .Destination}}{{end}}'

# Show available space and check the mount in the relevant worker (replace worker name if needed):
docker exec aetheris-forensic-worker-parse-1 sh -c 'df -h /app/data /tmp; ls -ld /app/data/tmp /tmp'

# Small non-destructive write/read probe, to be run only when no active repair is underway:
docker exec aetheris-forensic-worker-parse-1 python -c 'import tempfile,os; f=tempfile.NamedTemporaryFile(dir="/app/data/tmp",delete=False); p=f.name; f.write(b"polaron-storage-probe"); f.flush(); os.fsync(f.fileno()); f.close(); assert open(p,"rb").read()==b"polaron-storage-probe"; os.unlink(p); print("job temp mount read/write OK")'
```
The worker service name can be found with `docker ps --format "table {{.Names}}\t{{.Status}}"`.

Check Windows Event Viewer / storage and Docker Desktop WSL2 mounts for host disk, USB disconnect, filesystem damage, low disk space, or permission issues. The probe does not fix physical I/O errors. Do not delete evidence volumes or use `docker compose down -v`.

## Recovery conditions
After deploying the updated code, resume job `ec7f5e0d-658a-4e40-8d4f-15c97185d9a2` via Polaron's existing resume/recovery controls. Confirm the inventory stage reaches its validation barrier and records a committed result; never manually set a stage to 100%. If `/app/data` remains unhealthy, inventory reads/writes can still fail, requiring host storage repair or controlled relocation of the job data mount.

## Limitations and verification
5 focused isolated-stage I/O fault-injection checks passed. No live Docker/Windows disk or full forensic dataset was available, so live recovery is unverified. The source archive is based on the last updated project ZIP in this conversation, not a fresh snapshot of the user's Windows files.
