# Polaron V1.29.1: launches no longer reinstall libraries that are already installed

## What you saw
`start_docker_*.cmd` re-ran `[dependencies 5/5] RUN ... pip install torch torchvision ... -r requirements.txt` and
downloaded PyTorch's CUDA wheels again (for example `nvidia_cudnn_cu13 ... 553 MB`), although the libraries were
already installed.

## Why
- The launcher recognised "already installed" only through Docker's BuildKit **build cache**. Docker Desktop
  prunes that cache by itself:
  - cache mounts such as the pip download cache are removed after about 48 hours unused;
  - older entries are removed once the cache passes `defaultKeepStorage` (20 GB by default).
- The CUDA/PyTorch images of this project are much larger than 20 GB together. Once their entries were pruned, the
  dependency step ran again even though the image `aetheris-backend-deps` with every library was still on disk.
- `torch`/`torchvision` are installed without a version, so a re-run picks the newest wheels (torch 2.14.1 here).
  It downloads them again because the pip cache was pruned too.
- Nothing in V1.27-V1.29 changed `requirements.txt` or a library stage; the rebuild was caused by the cache pruning.

## What changed
1. **Library images are reused by fingerprint.** Each library stage is kept as a labeled image:
   - `aetheris-backend-deps`;
   - `aetheris-malware-deps`;
   - `aetheris-scanner-deps`;
   - `aetheris-frontend-deps`.

   The label is a SHA-256 fingerprint of what the stage installs: its Dockerfile lines (comments ignored), the files
   it copies in (requirements files, `package-lock.json`, `docker-pip-install.sh`) and `TORCH_INDEX_URL`.
   - If the fingerprint matches, the image is reused and nothing is installed or downloaded.
   - It is rebuilt only when a requirement file or a library stage really changes, or with `-RefreshDependencies`.
   - Application code changes never touch it.
2. **Application images start `FROM` the library image.** The four Dockerfiles have `ARG AETHERIS_DEPS_IMAGE`, and
   the application stage is `FROM ${AETHERIS_DEPS_IMAGE}`. The launcher passes the library image, so the library
   stage is never evaluated during an application build. Without the argument (other scripts, plain `docker
   compose build`), the default is the original stage, as before.
3. **Images from earlier releases are reused, not rebuilt.** On the first launch, an existing unlabeled
   `aetheris-backend-deps` / `aetheris-frontend-deps` / `aetheris-scanner-deps` is checked read-only:
   - it has the same base image layers;
   - its history has every `RUN` step and stage `ARG` of the current Dockerfile;
   - its copied requirement files have the same SHA-256;
   - for the scanner, it has the same Trivy version.

   If all checks pass, only a label is added: no layer changes and nothing is installed. A different recipe is never
   adopted.
4. **`-RefreshDependencies` still rebuilds everything from fresh bases.** It also pulls `nginx:1.27-alpine` for the
   website runtime. Application builds then reuse the refreshed library images instead of installing the libraries
   a second time.
5. **Other builders keep the previous behaviour.** If the active builder is not Docker Desktop's default `docker`
   driver (which can use local images), the launcher prints a warning and uses the previous cache-based path.

## Your current build
- **If you let the running launch finish,** it downloads these wheels this one time. The next launch with this
  update labels the new images and reuses them from then on.
- **If you stop it now,** the previous `aetheris-backend-deps` is still intact and is reused by the next launch.
  Application containers stay stopped until that launch.
- `aetheris-malware-deps` and `aetheris-scanner-deps` did not exist before (those stages were only inside the
  application builds). The first launch builds them once; this is instant when Docker's cache still has them. After
  that they are reused.

The launcher prints one line per library image, for example:
`Shared backend (...): libraries already installed in aetheris-backend-deps; reused, nothing downloaded.`

## Verification
- **Real Docker 29.9 (BuildKit, containerd image store as in Docker Desktop):** 28 checks with the
  project's real Dockerfiles. The base images were local stand-ins whose shell always succeeds, so every real `RUN`
  line is recorded exactly. The checks showed:
  - images built by the old Dockerfiles, after `docker builder prune -af`, are adopted with no `RUN` step and
    unchanged layers;
  - the next launch runs no `docker build` at all for them;
  - backend, website and malware application builds (plain build and `docker compose build --build-arg`) skip the
    library stage and sit on the library image layers;
  - without the argument, the Dockerfiles build from their own stage as before;
  - a changed `requirements.txt` rebuilds;
  - a changed pip command, another `TORCH_INDEX_URL` or another Trivy version is never adopted.
- **`tests/dependency_reuse_checks.ps1`:** 37 offline checks (real Dockerfiles parsed, Docker mocked), covering:
  - parsing of the real Dockerfiles;
  - when the fingerprint changes (requirements, apt list, `TORCH_INDEX_URL`) and when it does not (comments,
    application stage, CRLF);
  - every reuse/adopt/build decision;
  - launcher wiring, `-RefreshDependencies`, and builders other than the default `docker` driver.
- **pytest `test_dependency_reuse_v1_29_1.py`:** 10 checks. Among them, the library stages of all four Dockerfiles
  are proven unchanged against the pre-V1.29.1 snapshot, which is what lets existing images be reused.
- **Existing suites still pass:** startup 84 checks, with the same 3 known pre-existing failures; HTTPS edge 19;
  launcher pytest 22.

Not verified here: Docker Desktop on Windows itself, with your real images and GPU libraries.

## Rollback
Restore the files from `recovery-snapshots/pre-v1.29.1-dependency-reuse-20261115/`. Labeled images keep working
with the old launcher.
