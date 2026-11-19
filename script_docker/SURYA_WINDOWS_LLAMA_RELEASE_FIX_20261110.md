# Polaron — Windows llama.cpp binary release fix (10 November 2026)

## Root cause

The original `scripts/surya_host_bootstrap.py` queried only `https://api.github.com/repos/ggml-org/llama.cpp/releases/latest`. In November 2026 this points to the stable `v0.9.0` release, which has `nightly-tag.txt` but no Windows x64 `llama-server.exe` binary packages. The official numbered build releases, including `b12241`, do provide:

- `llama-b12241-bin-win-vulkan-x64.zip` (default Windows Vulkan backend)
- `llama-b12241-bin-win-cpu-x64.zip` (optional CPU backend)

The Surya GGUF and mmproj downloads in the reported log **succeeded**. They stay under `%LOCALAPPDATA%\Polaron\SuryaOCR\models` and do not need to be downloaded again. This issue is not a Surya recognition model error.

## Fix

- Detect the requested Windows x64 backend from the **recent GitHub releases listing**, skipping source-only stable releases.
- Use the official `b12241` build package as a fallback when GitHub release discovery is unavailable or rate-limited. No arbitrary third-party ZIPs are allowed.
- Check downloaded size and optionally the SHA256 released by GitHub; validate ZIP CRC, expected `llama-server.exe`, runtime DLLs, maximum archive size, and path traversal.
- Extract to temporary staging; only publish the new executable directory after validation. Retain already-installed models and a valid existing cached server.
- Do not silently substitute CPU for a Vulkan installation. For CPU mode, set the Windows environment variable `SURYA_HOST_ACCELERATOR=cpu` and `SURYA_HOST_GPU_LAYERS=0`.
- Improve error information from the PowerShell launcher.

## Apply the patch on Windows

1. **Keep your existing `.env`, evidence, Postgres/MinIO/Neo4j data, and cached Surya models.** Do not delete the `%LOCALAPPDATA%\Polaron\SuryaOCR` folder.
2. Extract `Polaron_Surya_Llama_Binary_Fix.zip` to your Polaron root, e.g. `D:\project1\polaran2`, preserving the `scripts` and `backend` directory structure. Only the installer, host manager and tests are replaced/added.
3. To test just the Surya host installation, open **PowerShell**:

```powershell
cd D:\project1\polaran2
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\ensure_host_surya.ps1 -ProjectRoot 'D:\project1\polaran2'
Invoke-RestMethod http://127.0.0.1:8000/v1/models
```

4. Then start Polaron as usual from CMD:

```cmd
cd /d D:\project1\polaran2
script_docker\start_docker_local.cmd
```

For production use `script_docker\start_docker_prod.cmd` instead.

### Network fallback

If the GitHub API is rate limited, the installer uses the official known build automatically. If GitHub release asset downloads are blocked by corporate policy, download the correct ZIP directly from:

- Vulkan: https://github.com/ggml-org/llama.cpp/releases/download/b12241/llama-b12241-bin-win-vulkan-x64.zip
- CPU: https://github.com/ggml-org/llama.cpp/releases/download/b12241/llama-b12241-bin-win-cpu-x64.zip

Extract the entire release ZIP (not only the `.exe`) to a directory so its `.dll` files remain adjacent. Set an override **for the same Windows session** before launching:

```powershell
$env:SURYA_LLAMA_SERVER_EXE = 'C:\Tools\llama.cpp\llama-server.exe'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\ensure_host_surya.ps1
```

You can also pin a tested numbered build in your Windows environment:
`$env:SURYA_LLAMA_BUILD_TAG = 'b12241'`. The installer then looks up only that official build. Use this for reproducibility; do not treat nightly build releases as long-term supported releases.

### Troubleshooting

- **GitHub 403 / rate limit:** fallback to numbered build is automatic. If download fails, use the direct official URL above and set `SURYA_LLAMA_SERVER_EXE`.
- **Windows Defender quarantines EXE:** inspect Windows Security; only allow verified files from the official ggml-org releases.
- **Vulkan/DLL error:** check Windows GPU driver and confirm all DLLs were extracted next to the executable. For a CPU test, set accelerator to `cpu`.
- **Model busy / Ollama memory pressure:** reduce `SURYA_HOST_GPU_LAYERS` (12 by default), tune Ollama loaded models, or use CPU mode.
- **Failed inference startup:** inspect `%LOCALAPPDATA%\Polaron\SuryaOCR\surya-server.err.log`.
- **Port 8000 occupied:** set `SURYA_HOST_PORT` consistently with the `SURYA_INFERENCE_URL` Docker setting.

## Validation

Offline regression checks: **48 tests passed** (including new GitHub release selection, SHA256 verification, ZIP install safety, cached binary reuse, Surya OCR, specialist routing and disk multi-directory guard tests). Python source compile passed. Windows PowerShell execution and live llama.cpp inference could not be tested here. Verify `/v1/models` and a real OCR page on your machine before treating OCR as production-ready.

Reference: https://github.com/ggml-org/llama.cpp/releases/tag/b12241
