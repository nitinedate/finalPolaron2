# Polaron Surya-only OCR transition — 10 November 2026

## Scope

Based on `polaron2_host_ai_rag_hardened.zip` from the previous release. This is a modification of the existing project, not a greenfield rewrite. The primary OCR engine is SuryaOCR through a separately hosted vLLM endpoint; Tesseract remains available on explicit selection. Ollama stays installed on the host and is used for the selected LLM/reranker/embedding tasks, not OCR.

## Updated live paths

- `backend/app/services/ocr_gpu.py`: removed local GLM model loading/inference and replaced it with a Surya-only adapter. Removed the GLM model memory residency state and replaced cleanup hooks with external-client no-ops. Strict page count and request failure validation ensure partial OCR is not mistaken for full coverage. `OCR_ENGINE=glm-ocr` is explicitly invalid.
- `backend/app/services/surya_adapter.py`: existing external-Surya integration is retained; it checks for a reachable configured vLLM service and preserves page order.
- `backend/app/config.py`, Compose files for the main stack and mobile stacks, `.env.example`: configure Surya model and service URI without the removed model.
- `scripts/setup_host_ai.ps1`, `scripts/setup_host_ai.sh`, `scripts/check_host_ai.py`, `scripts/ollama_pull_models.ps1`, `scripts/ollama_pull_models.sh`, `scripts/hf_ensure_models.py`: no GLM downloads; legacy model lists are filtered rather than silently fetching the old model.
- `backend/app/services/forensic_media_review.py`: does not claim OCR is a vision scene description system. The vision model must be explicitly configured; no fabricated image findings if absent.
- `backend/app/services/live_acceptance.py`: validates recognition of the known sample marker from external Surya and does not claim to have proven GPU residency on the remote inference server.
- `backend/app/services/gpu_thermal.py`, `backend/app/celery_factory.py`, `backend/app/services/forensic_serial_stages.py`, `backend/app/services/embedding_gpu.py`: updated references to the external client cleanup semantics.
- `scripts/migrate_surya_only_env.py`: dry-run-first migration of existing `.env`. `--apply` backs it up and preserves unrelated settings; backup itself contains secrets and must be protected.
- `backend/tests/test_surya_only_ocr.py`, `backend/tests/test_migrate_surya_only_env.py`: regression coverage including engine rejection, Surya page ordering, page-count mismatch, backend failure, timeout/partial OCR safeguards, Tesseract fallback selection and preservation of unrelated `.env` data.

## Validation in this build environment

| Scenario | Status | Notes |
| --- | --- | --- |
| Python syntax for active backend and scripts | PASS | `python -m compileall -q` |
| Main and HTTPS Compose YAML parsing | PASS | YAML parser, 16 and 21 services respectively |
| Mobile extract, Android and iOS Compose YAML parsing | PASS | YAML parser, 10, 12 and 12 services respectively |
| Surya OCR + migration + existing offline host-AI tests | PASS | 15 tests (`6 + 2 + 7`) |
| Larger focused subset: Surya, migration, host-AI, media, mobile | PARTIAL | 25 passed; seven older test expectations failed (removed media-review CUDA reader API and mobile report section ordering), independent of a real Surya server |
| Malware sandbox and remediation tests | PARTIAL | Earlier run: 29 passed, 3 skipped, 3 setup errors due to missing `psycopg2` in this sandbox |
| Live Surya vLLM inferencing | NOT RUN | No target model server available here |
| Host Ollama against all configured models | NOT RUN | No access to target workstation |
| PostgreSQL case indexing, migration, source hashing and report export end-to-end | NOT RUN | Requires deployed database and representative image evidence |
| 100 GB image within 90 minutes | NOT PROVEN | Requires measured end-to-end benchmark with case and page coverage |
| Malware execution isolation and post-remediation state on physical endpoints | NOT PROVEN | Requires controlled lab, independent security review and authorized test corpus |

The untested deployment paths must be validated before production use. OCR 100% means completion of the scoped queued work, not 100% source or deleted-data coverage. Never alter original evidence when remediating a live endpoint. Separate pre/post-remediation reports and preserve hashes and chain of custody.

## Existing-workstation transition

```powershell
# At project root. Review planned changes without editing .env:
py -3.12 scripts\migrate_surya_only_env.py --env .env
# Apply after review (creates a timestamped .env backup):
py -3.12 scripts\migrate_surya_only_env.py --env .env --apply
# Host-level Ollama check. No GLM OCR pull:
ollama list
.\scripts\setup_host_ai.ps1
# Check host inference; for container use host.docker.internal rather than localhost:
$env:SURYA_INFERENCE_BACKEND = 'vllm'
$env:SURYA_INFERENCE_URL = 'http://localhost:8000/v1'
py -3.12 scripts\check_host_ai.py
# Validate Compose and run offline tests:
docker compose config
py -3.12 -m pytest -q backend/tests/test_surya_only_ocr.py backend/tests/test_migrate_surya_only_env.py backend/tests/test_host_ai_rag_offline_v1.py
```

If the external Surya service cannot run on the same GPU as other models, allocate dedicated GPU or isolate inference and share admission control; the existing GPU semaphore does not guarantee protection against unrelated host processes. Surya model licensing and model-specific deployment requirements must be verified for commercial forensic use.

## Archived documentation and test fixtures

Older project release notes and inactive patch archives may reference the superseded GLM model. They are retained for historical audit only and are **not** instructions for deploying this release. The generated `merged-compose.txt` snapshot from the superseded version is removed from the delivery to avoid accidental reuse. Synthetic WhatsApp key fixtures used by forensic tests are retained; no runtime `.env` or private credentials are included.
