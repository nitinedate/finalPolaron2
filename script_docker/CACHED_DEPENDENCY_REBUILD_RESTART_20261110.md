# Polaron: cached packages, source rebuild, application restart

Release: 2026-11-10 | Applies to Windows local and production CMD launchers.

## Normal startup (both profiles)

1. Validate the project `.env`, pgAdmin email, Docker Desktop, generated drive mounts, and Compose definitions.
2. Verify the native host SuryaOCR service or configured external endpoint without installing it again if it is healthy.
3. Inventory locally tagged required Docker build bases (CUDA, Python, Node, nginx, Trivy, ClamAV) and **default-profile** external service images. If one is missing, fetch **only the missing image before stopping Polaron**. Optional Greenbone feed and scanner images are not globally pre-pulled.
4. Stop **only Polaron application containers** (forensic, mobile Android/iOS, malware, vulnerability API/worker, gateway), with a bounded graceful shutdown. Do not stop Common Docker, long-running Greenbone scanner engines/feeds, or the HTTPS certificate renewer.
5. Always run the build pipeline for current sources using Docker BuildKit. Unmodified Dockerfile dependency layers, pip wheels, npm lockfile installation, APT packages, and PyTorch/CUDA installation are reused from local build caches; unchanged source compilation stages are also reused. **No `--pull` or `--no-cache` flags** are applied on an ordinary run.
6. Start Polaron applications from locally built images (`compose up -d --no-build --pull missing`), while reusing any healthy shared services. Existing database, MinIO, Redis, and forensic evidence volumes remain unchanged.
7. For production, reuse a healthy certificate renewer and stored valid TLS certificate instead of repeating ACME. For Greenbone, reuse healthy long-running scanner engines and cached feeds instead of reinitializing them.
8. Verify API, gateway, OCR connectivity and container readiness.

### Important limitations

- The first run, an image pruned by Docker, or an updated dependency manifest may need a download or package installation. Package layer reuse requires that Docker Desktop / BuildKit caches remain intact.
- The normal restart **interrupts active forensic jobs**. Schedule it when no disk/mobile acquisition, extraction or case processing is actively running. A restart preserves evidence files and Docker volumes, but in-flight tasks may be interrupted.
- If a source build fails after the application containers are stopped, startup fails clearly and containers may remain stopped; check the build error and correct the code or restore the last good image. Shared Common Docker remains running.
- NVIDIA drivers are host-level, not shared by a running Docker library service. CUDA base image layers and downloaded wheels are reused by Docker; GPU inference is still scheduled per worker.
- A pinned Docker image / lockfile is recommended for audited repeatable production deployments. Existing floating tags may update on explicit refresh.
- Health probes for shared services may require a repair when the service itself is unhealthy; this is separate from normal application restarts.

## Commands

Run from `D:\project1\polaran2`:

```cmd
script_docker\start_docker_local.cmd
script_docker\start_docker_prod.cmd
```

Optional checks and maintenance:

```cmd
script_docker\start_docker_local.cmd -ValidateOnly
script_docker\start_docker_local.cmd -PrepareOnly
script_docker\start_docker_local.cmd -RefreshDependencies
script_docker\start_docker_prod.cmd -Recreate
```

`-RefreshDependencies` deliberately downloads/rebuilds dependencies (not for routine startup). `-Recreate` explicitly replaces Polaron-owned containers; it does not remove named volumes. Do not run `docker system prune`, `docker volume prune`, or `docker compose down -v` on forensic production evidence.

## Installation of patch

Extract the patch ZIP **into the root** `D:\project1\polaran2`, preserving paths, so these three files replace the old versions:

- `script_docker/start_docker_local.cmd`
- `script_docker/start_docker_prod.cmd`
- `script_docker/start_docker.ps1`

The supporting `start_docker.lib.ps1` and Compose files from the previous full release remain compatible. No `.env` is included in the patch and no evidence or volumes are modified by extraction.

## Verification performed

- Offline structural test suite for cached preflight, stop scope, build pipeline, production certificate and Greenbone reuse, legacy pgAdmin repair and Surya setup.
- Windows CMD and PowerShell files are ASCII with CRLF line endings.
- YAML parsed with Compose override tags allowed.
- No live Windows Docker Desktop or Nvidia GPU integration test could be executed in the build container.
