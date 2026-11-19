# Polaron SuryaOCR — Windows llama.cpp installation WinError 5 repair

Date: 10 November 2026

## Observed error

```text
[Surya] Windows llama.cpp build selected: llama-b12242-bin-win-vulkan-x64.zip
[Surya] Downloading official Windows llama.cpp package: llama-b12242-bin-win-vulkan-x64.zip
[Surya ERROR] [WinError 5] Access is denied: '...\\llama-setup-...\\runtime' -> '...\\llama.cpp'
```

This error occurs **after** both Surya model files were cached and after downloading the correct official llama.cpp Windows package. The prior bootstrap tried to rename a newly extracted directory into the fixed `llama.cpp` directory. Windows may deny directory replacement due to existing folders, filesystem/antivirus file handles, or access controls. The attempted directory rename is unnecessary.

## Actual code changes

- `scripts/surya_host_bootstrap.py`: install each validated `llama-server.exe` and its DLLs **directly into a new private persistent folder** under `%LOCALAPPDATA%\Polaron\SuryaOCR\llama-runtimes\`, avoiding `os.replace(stage, llama.cpp)` and avoiding deletion of existing `llama.cpp`.
- `scripts/surya_host_bootstrap.py`: write `.polaron-runtime.json` only after successful unpacking, to mark successful installs. Ignore incomplete folders; reuse a verified, backend-matching runtime on subsequent startups. Retain support for a valid legacy `llama.cpp\llama-server.exe` or administrator-set `SURYA_LLAMA_SERVER_EXE`.
- `scripts/surya_host_bootstrap.py`: download and unpack the ZIP within its private install folder; a Windows file lock preventing ZIP removal is logged rather than aborting an otherwise valid installation.
- `scripts/surya_host_bootstrap.py`: support `--result-file` for machine-readable paths (model, mmproj, executable).
- `scripts/ensure_host_surya.ps1`: read the **actual executable path** returned by the Python installer instead of assuming `%LOCALAPPDATA%\Polaron\SuryaOCR\llama.cpp\llama-server.exe`. Maintain startup health checks and normal host service launch.
- `backend/tests/test_surya_host_bootstrap.py`: regression tests for a locked legacy folder (`PermissionError(5)` on directory rename), reuse after restart, partial install detection, and correct PowerShell integration.

No source-evidence files, case databases, model weights, or Docker volumes are deleted or modified.

## Apply on Windows (recommended: small patch)

1. Close the Polaron launcher window that displayed the error. Keep Docker evidence volumes and **do not** delete `%LOCALAPPDATA%\Polaron\SuryaOCR`.
2. Extract `Polaron_Surya_WinError5_Fix.zip` **into the project root**, for example `D:\project1\polaran2`, preserving `scripts\...` paths and replacing the two scripts. No model download or pip reinstall is necessary.
3. Test the host service on its own from PowerShell:

```powershell
cd D:\project1\polaran2
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\ensure_host_surya.ps1 -ProjectRoot 'D:\project1\polaran2'
Invoke-RestMethod http://127.0.0.1:8000/v1/models
```

4. If the `/v1/models` response contains the Surya model and no provisioning error is displayed, start Polaron normally:

```cmd
cd /d D:\project1\polaran2
script_docker\start_docker_local.cmd
```

Production: `script_docker\start_docker_prod.cmd`. Both launchers already invoke `scripts\ensure_host_surya.ps1` through `start_docker.ps1`.

## Expected output on successful first install

```text
[Surya] Reusing existing model weight: surya-2.5.gguf
[Surya] Reusing existing model weight: surya-2.5-mmproj.gguf
[Surya] Windows llama.cpp build selected: llama-b12242-bin-win-vulkan-x64.zip
[Surya] Downloading official Windows llama.cpp package: ...
[Surya] Installed llama.cpp server without directory replacement: ...\llama-runtimes\...\llama-server.exe
[Surya] Selected llama.cpp executable: ...\llama-runtimes\...\llama-server.exe
[Surya] Starting Windows host llama.cpp server ...
[Surya] Host inference ready.
```

On the next startup, the `Reusing verified cached llama.cpp server` message appears and the server package is not downloaded again.

## If an Access Denied error still occurs

A *different* path in the error may indicate Windows Defender quarantine, policy-controlled folders, or a process holding the downloaded runtime. Check **Windows Security > Protection history** and your organization's endpoint protection alerts before making exceptions. Do not disable antivirus globally. Ensure that your current Windows account has write access to `%LOCALAPPDATA%\Polaron\SuryaOCR`. You can also install the official Windows `llama.cpp` ZIP in a trusted directory with the adjacent DLLs and configure `SURYA_LLAMA_SERVER_EXE` to point at its `llama-server.exe` (do not point it at the ZIP). The Surya model cache remains unchanged.

For a later server-start error rather than installation error, inspect `%LOCALAPPDATA%\Polaron\SuryaOCR\surya-server.err.log`. Verify with a real OCR page; `/v1/models` alone confirms only API/model readiness.

## Validation performed

- 50 focused offline regression tests passed covering Surya bootstrap, Surya OCR, environment migration, multi-directory extraction barriers, model specialists and host AI.
- Python bootstrap compilation passed.
- The fixed scripts and ZIP packages were checked after creation.

**Not tested:** Real Windows x64 filesystem/antivirus behavior, live Vulkan `llama-server.exe` execution, Ollama/Surya GPU concurrency, and actual Docker OCR inference. Those require your Windows workstation.
