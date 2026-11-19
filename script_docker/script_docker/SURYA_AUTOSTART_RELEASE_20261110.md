# Polaron Windows SuryaOCR automatic host startup (2026-11-10)

## What changed

- `script_docker/start_docker_local.cmd` and `start_docker_prod.cmd` use `start_docker.ps1`, which now provisions SuryaOCR **before** any Polaron product containers are stopped or rebuilt. The standalone host Surya launcher is `scripts/ensure_host_surya.ps1`.
- `scripts/align_surya_env.ps1` migrates **only known default** `SURYA_INFERENCE_BACKEND=vllm` and `SURYA_INFERENCE_URL=http://host.docker.internal:8000/v1` settings to the native Windows `llamacpp` backend; backs up existing `.env` to `.env.surya-pre-native.bak`. A custom endpoint is kept as external. Other settings and credentials are not rewritten.
- `scripts/surya_host_bootstrap.py` downloads the official Surya 2.5 GGUF and multimodal projection weights from `datalab-to/surya-ocr-2.5-gguf`, plus a Windows x64 llama.cpp release server. Downloads and Python dependencies are cached under `%LOCALAPPDATA%\Polaron\SuryaOCR` by default.
- A Windows Python 3.12 virtual environment installs SuryaOCR and `huggingface_hub`; a native llama.cpp server is started and kept running for future launches. The model is downloaded only if absent. There is **no Polaron Compose Surya server or Ollama server**.
- The backend Surya OCR adapter supports external `vllm` and `llamacpp` OpenAI-compatible `/v1` endpoints, with explicit failure if unreachable.
- The launch checks host `/v1/models` before stopping existing product containers; once the forensic API container starts, it checks the endpoint again from Docker using `host.docker.internal`. If it fails the startup reports an error rather than claiming success.
- `.env.example` defaults to the Windows `llamacpp` backend. Main Compose files accept the same backend. Other Docker, Neo4j, RAG, malware, vulnerability, multi-directory extraction, specialist AI and evidence settings remain intact.

## Installation

1. Back up the Polaron folder and `.env`. Extract the updated project over your installation (`D:\project1\polaran2`) **or** copy the patch ZIP into the root, preserving paths.
2. Install Python **3.12** with the `py` launcher, Docker Desktop (required for Polaron), a current NVIDIA graphics driver if using GPU inference, and sufficient disk space for Surya model weights and pip dependencies.
3. Set `SURYA_HOST_MODE=native` (default) in `.env`. Native Windows server uses port 8000 and the Vulkan llama.cpp release by default. Optionally set `SURYA_HOST_ACCELERATOR=cpu` and `SURYA_HOST_GPU_LAYERS=0` in the Windows environment if GPU inference is unstable. The GPU layer count defaults to 12, configurable via `SURYA_HOST_GPU_LAYERS` Windows environment variable.
4. Run `script_docker\start_docker_local.cmd` or `script_docker\start_docker_prod.cmd`. The first startup will download and install dependencies and models; later starts reuse the existing cache/server.
5. Confirm host endpoint: `Invoke-RestMethod http://127.0.0.1:8000/v1/models`. Docker connectivity is validated by the launcher after product startup.
6. If installation fails, inspect `%LOCALAPPDATA%\Polaron\SuryaOCR\surya-server.err.log`. Correct the underlying error and run again. Existing Polaron containers are not stopped when host Surya provisioning fails.

### Existing external vLLM deployment

Set `SURYA_HOST_MODE=external` in `.env` and retain the custom `SURYA_INFERENCE_URL` and `SURYA_INFERENCE_BACKEND=vllm`. In this mode the launcher does not install the native llama.cpp server, but still checks Docker-side reachability. Do not point two model servers at the same port.

### Skip OCR

`SURYA_HOST_MODE=disabled` skips the host OCR bootstrap and reachability check. This is an explicit degraded configuration; it cannot claim complete OCR coverage. It does not change artifact/report integrity safeguards.

### Other operational notes

- The Surya model is distinct from Ollama models; the two specialist models remain installed and managed by host Ollama.
- Native `llama.cpp` can run with CPU offload and Vulkan GPU acceleration. VRAM usage depends on model quantization, context length and GPU layers; on 12 GB GPUs tune `SURYA_HOST_GPU_LAYERS` and avoid concurrently keeping large Ollama models loaded. This is **not** a validated 100 GB / 90-minute configuration.
- The llama.cpp process listens on `0.0.0.0` so Docker Desktop can reach `host.docker.internal`. **Restrict inbound Windows firewall access to TCP 8000 to trusted local/Docker networks; do not port-forward it or expose it publicly.** In regulated settings deploy an authenticated internal proxy before any broader network access.
- First-run dependency/model installation requires internet access, and model downloads can be several GB. This environment could not perform live Windows installation, NVIDIA execution, Docker network tests or a full forensic workload benchmark.
- Commercial users should review the Surya model-weights license; the open-source Surya code and separately licensed model weights have different licensing terms.

## Test results

Focused Python tests: `21 passed` (`test_surya_host_bootstrap.py`, `test_surya_only_ocr.py`, `test_host_ai_rag_offline_v1.py`, `test_ollama_model_plan.py`). Python compilation and packaged archive integrity checked. PowerShell runtime parsing, Windows process creation, GPU inference, model download and end-to-end OCR still require testing on the target Windows host.

## Key source references

- https://github.com/datalab-to/surya
- https://huggingface.co/datalab-to/surya-ocr-2.5-gguf
- https://github.com/ggml-org/llama.cpp/releases
