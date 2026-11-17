# Polaron V1.31.12: Surya OCR in Docker starts on Windows PowerShell 5.1

## What was reported

    [Surya] Smart App Control is on: llama.cpp's unsigned Windows build cannot run on this computer. Using the same server in Docker.
    ...
        SuryaOCR could not be started: error: no such object: polaron-surya-ocr
        Polaron starts WITHOUT OCR ...

Detecting Smart App Control worked (V1.31.11), and Polaron started without OCR instead of stopping (V1.31.10).
But the Surya container was never created.

## Cause
- **The first step of the Docker path failed.** Before creating the container, the manager asks Docker whether
  a Polaron Surya container is already running. On a first run Docker answers on stderr with
  *Error: no such object: polaron-surya-ocr*, which is the expected answer.
- **Windows PowerShell 5.1 turns that into a stop.** It treats a program's redirected stderr as error records.
  Because the script runs with `ErrorActionPreference = Stop`, that harmless answer ended the script.
- **Why the tests missed it.** They ran on PowerShell 7, which does not do this, and their stand-in for Docker
  never wrote to stderr.

## What changes
- `scripts\ensure_host_surya.ps1`: every Docker call (inspect, info, rm, pull, run, logs) goes through one
  function, `Invoke-Docker`. In it:
  - stderr is ordinary text and only Docker's exit code decides;
  - `docker pull` progress is shown;
  - a failed pull or run is shown with Docker's own message before the next image is tried.
- The test stand-in for Docker now writes its errors to stderr the way Windows PowerShell 5.1 receives them. The
  same test fails against the V1.31.11 script, reproducing the report, and passes against this one.

## What you do
1. Copy `POLARON_V1_31_12_UPDATED_FILES.zip` over the Polaron folder on this computer (it contains V1.31.0 to
   V1.31.12). Only `scripts\ensure_host_surya.ps1` changed since V1.31.11.
2. With Docker Desktop running, run the launcher again. The Surya step then:
   - pulls `ghcr.io/ggml-org/llama.cpp:server-cuda` (NVIDIA GPU) or `:server` (CPU) the first time, with
     progress shown;
   - starts the container `polaron-surya-ocr`;
   - says **Host inference ready (Docker container polaron-surya-ocr)**.

   The next step, *Verify Surya inference is reachable from forensic API container*, confirms the forensic
   containers can use it.

   To check Surya alone first:
   `powershell -ExecutionPolicy Bypass -File scripts\ensure_host_surya.ps1`

## Tests
- `tests/surya_app_control_checks.ps1`: 31 checks (1 new). The Docker stand-in now reports errors on stderr as
  5.1 does. The new check confirms every Docker call goes through `Invoke-Docker`.
- The same file run against the V1.31.11 manager fails at the first Docker step, as reported.
- `backend/tests/test_surya_host_bootstrap.py`: 17 pass.
- Not run on Windows here.

Originals: `recovery-snapshots/pre-v1.31.12-surya-docker-ps51-20261117/`.
