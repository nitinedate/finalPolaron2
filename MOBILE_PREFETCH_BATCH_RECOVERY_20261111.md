# Polaron Mobile Native Parsing: large-shard prefetch efficiency patch

## Findings
An iOS job showed `Prefetch shard: scanned 15,244 files` while native parsing advanced only a few hundred bundles. `shard_prefetch.choose_prefetch_groups()` used to spend its 64 MiB budget on encountered source metadata before ranking shard groups. When claims were scattered, that selection could omit useful dense shard groups and repeatedly pay for costly `.tar.zst` scans.

## Changes
1. Rank all candidate source metadata by extraction part before allocating prefetch bytes; select up to `MOBILE_PREFETCH_MAX_SHARDS` parts; enforce the existing 64 MiB per-worker cache cap.
2. Increase default `MOBILE_PARSE_CLAIM_BATCH` from 24 to 256 (configurable 1..512). Per-bundle database commits, fingerprints, integrity verification, error accounting, claim release and source coverage remain unchanged. A bigger window is intended to amortize a single expensive shard scan across more eligible files, at a cost of more temporary claimed work per worker.
3. Add `matched X / Y requested` alongside `scanned N files` in progress operations, so high-cost low-yield shard reads are visible.

## Installation
- First verify all active forensic jobs have reached a safe pause/checkpoint and preserve PostgreSQL/MinIO/evidence backups. Do not run `docker compose down -v`, `volume prune`, or restart a source acquisition.
- Back up existing backend/app/services/forensic_serial_mobile.py and backend/app/services/mobile_forensic/shard_prefetch.py.
- Extract the patch ZIP to the existing project root, preserving paths.
- Rebuild/recreate ONLY the image and worker services that execute Mobile `parse`/`recovery`, using the project's current compose files/launcher. Restart of the API may be required if this code is also imported there. Preserve all persistent volumes and existing jobs.
- Resume the same case; newly launched workers use the updated claim batch, while previously committed work remains committed.

## Configuration and rollback
Defaults: `MOBILE_SHARD_PREFETCH=true`, `MOBILE_PARSE_CLAIM_BATCH=256`, `MOBILE_PREFETCH_MAX_SHARDS=2`, `MOBILE_PREFETCH_MAX_BATCH_BYTES=67108864`.
If RAM/disk pressure or long claims appear, change `MOBILE_PARSE_CLAIM_BATCH=64` or `24` and recreate the parsing worker after a safe pause. Set `MOBILE_SHARD_PREFETCH=false` only as a diagnostic fallback because it can cause repeated full-archive scans for each file and thus be slower. Changes to `.env` alone have no effect unless Compose passes them into the worker container.

## What this patch does NOT fix
No benchmark was run on the live 179,028-bundle iOS case. `tar.zst` archives are not randomly seekable: 15,244 members scanned to find a handful of files still costs decompression and may remain the main bottleneck. The long-term solution is a forensic-aware one-pass shard executor (or validated seekable/indexed evidence storage with bounded local spool) that groups all required parser work for a shard, including SQLite database plus WAL/SHM handling. This patch is an incremental optimization, not proof of a 10x improvement.

## Verification
- `pytest -q backend/tests/test_mobile_shard_prefetch_20261111.py` (8 passed)
- Python syntax compilation for both changed modules
- Validate extraction manifests, queue counts, original file hashes, SQLite/sidecar grouping and no duplicate artifacts under a real test case before broad deployment.
- Track *committed bundles/minute* over 10-20 minute windows and `mobile-parse-perf prefetch_s parse_s commit_s` logs, not only UI percentage.

## Safety
Do not mark the process complete on a percentage alone, delete temporary acquisition or evidence objects, or skip unsupported/deleted records without coverage accounting.
