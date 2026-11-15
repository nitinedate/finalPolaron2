# Polaron — MinIO registry and cached startup fix (2026-11-10)

## Failure addressed

The earlier launcher eagerly pulled `cgr.dev/chainguard/minio:latest`. On the reported network, that registry redirected to Cloudflare R2 and Docker received plaintext HTTP where it expected HTTPS. This occurs **before** MinIO is started; it is not an error in forensic processing or the Surya model cache.

The shared stack now defaults to the **same publisher's Docker Hub image**, `chainguard/minio:latest`, available from the official `chainguard` Docker Hub organization. This changes only the source of the container image; it does not reset buckets, accounts, or the bind-mounted `/data` location. An existing healthy `aetheris-common/minio` container is reused as-is. The launcher also detects a locally cached `cgr.dev/chainguard/minio:latest` image and can tag it for the Docker Hub name without downloading new layers. Override with `COMMON_MINIO_IMAGE` for a verified internal image mirror. Because `latest` is a moving tag, pin an internally verified digest for audited production deployments.

## Faster startup and GPU organization

- **No global pre-pull:** normal startup no longer downloads a list of ~30 unrelated NVIDIA, database, Greenbone and web-scanner images. It requests only images actually needed for the chosen Compose services, with Docker's native `--pull missing` caching.
- **No duplicate rebuild:** both local and production launchers inspect existing application image labels against a stable source build ID. When matching images exist, the build step (including pip, npm, Torch and CUDA base-image checks) is skipped. If the source/dependency manifest changes, relevant BuildKit image layers are reused.
- **No mandatory product shutdown:** a normal launch reconciles running containers through Compose rather than first stopping every Polaron project. Updating changed images can still recreate affected services, so schedule code deployments between active evidence jobs.
- **Preserve shared services:** healthy PostgreSQL/pgvector, Redis, MinIO, OpenSearch, pgAdmin and MailHog are not restarted. The installer will not reclaim ports by stopping foreign containers or Windows processes.
- **GPU:** removed duplicate permanently-running `nvidia-cuda` healthcheck containers and their unnecessary dependency edges from disk, Android, iOS and legacy mobile-extract. A single *optional* `nvidia-gpu-check` diagnostic lives in the Common Docker Compose under profile `gpu-check`. It is not started by normal launches. GPU workers continue to use Docker's `gpus: all`, which accesses the host NVIDIA driver/Container Toolkit directly; no driver can be “loaded once into Common Docker” and shared as a GPU process.
- **Windows/HTTPS:** when the existing public gateway, renewer and certificates are all valid and application images did not change, the launcher keeps them running and skips ACME bootstrap/recreation. If a cert is nearing expiry or invalid, the normal renewal workflow is retained.
- **Greenbone:** when its API, worker and primary scanner services are already running, skip repeated large feed initializer jobs. Reinitialize if a core engine is missing/degraded or maintenance is requested.
- **MinIO liveness:** replace a Bash-based container healthcheck (not guaranteed in a minimal image) with a host-side HTTP check on `/minio/health/live`.
- **Safety:** no `docker system prune`, `docker volume prune`, `docker compose down -v`, or deletion of evidence/storage volumes.

## Apply the patch

1. Back up your project scripts and Compose configuration. Finish or checkpoint active forensic jobs before deploying changed images.
2. Extract `Polaron_Fast_Startup_MinIO_GPU_Patch.zip` **into the existing project root** (`D:\project1\polaran2`), preserving the ZIP directory structure. **Do not overwrite the existing `.env`**; only `.env.example` files are included.
3. Run the configuration check:

```cmd
cd /d D:\project1\polaran2
script_docker\start_docker_local.cmd -ValidateOnly
```

4. Start the local or production profile:

```cmd
script_docker\start_docker_local.cmd
script_docker\start_docker_prod.cmd
```

`start_docker_prod.cmd` uses your existing saved public HTTPS settings. Do **not** start the local and production profiles simultaneously against the same gateway ports.

### Explicit maintenance flags

```cmd
:: Recreate only Polaron-owned containers (never shared Common Docker):
script_docker\start_docker_local.cmd -Recreate

:: Refresh Python/npm/OS dependencies and rebuild images (slow; intentional):
script_docker\start_docker_prod.cmd -RefreshDependencies

:: Troubleshoot a Docker registry proxy by pulling required images serially:
set AETHERIS_SEQUENTIAL_PULL=1
script_docker\start_docker_local.cmd

:: Optional maintenance-only prefetch of all library/vendor images:
set AETHERIS_PREFETCH_ALL_IMAGES=1
script_docker\start_docker_prod.cmd -RefreshDependencies
```

Optional GPU check (run from the project root, after Common Docker starts):

```cmd
docker compose -p aetheris-common -f common-docker\docker-compose.yml --profile gpu-check run --rm nvidia-gpu-check
```

## If MinIO cannot be downloaded

1. Check for misconfigured Docker Desktop proxy or HTTPS inspection. The error `server gave HTTP response to HTTPS client` can be caused by a firewall/proxy; switching registry does not fix every network policy.
2. Confirm the image source: `docker pull chainguard/minio:latest`.
3. If you already have a trusted private registry mirror, set `COMMON_MINIO_IMAGE=your.registry.example/minio@sha256:...` in the project's existing `.env` instead.
4. Never point MinIO at a new/empty data directory simply to bypass a startup error. Preserve the existing `/data` bind mount and backup evidence before changing object-store versions.

## Validation and limitations

- YAML parse of 29 project Compose files passed, with Compose override/reset tags handled in the syntax pass.
- 43 focused backend/startup/HTTPS tests passed, including legacy sidecar topology, source-cache checks and TLS tests using a correctly constructed test CA.
- No `docker` or Windows PowerShell executable is installed in this build environment; **live Docker Compose config, GPU access, MinIO registry pulls, Windows CMD execution and end-to-end services have not been verified**.
- The launchers cannot make unrelated physical Docker images share a single Python virtual environment. Docker content-addressed layers are shared automatically; application-specific packages remain in their own images when their dependency sets differ.
