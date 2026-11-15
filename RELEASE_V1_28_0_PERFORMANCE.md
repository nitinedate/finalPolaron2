# Polaron V1.28.0: faster evidence reads for native parsing, recovery, OCR, RAG and artifact preview (same results)

## What was slowing everything down

Every stage that reads extracted evidence goes through one function, `tar_cache.iter_files_from_part()`:
- native forensic parsing (disk and mobile)
- deleted / recovery analysis
- GPU OCR and quality triage
- image/video review
- inventory counts
- RAG chunking
- artifact preview ("getting artifacts")

Extraction stores each shard as **one zstd frame around a tar stream**. On this server the Android job's 8 parts are
about 2 GB each, and the 142 GB iPhone export gives parts of about 18 GB. Two problems compounded:

1. **Shards cannot be seeked.** Reading one file meant streaming the shard from MinIO and decompressing it **from
   byte 0** until that file appeared.
   - Mobile parsing, recovery and preview read files in many separate calls, so the same shard was decompressed
     again and again.
   - This matches the earlier logs: 64,214 / 179,028 bundles took 3 h 11 m, and a prefetch "scanned 15,244 files"
     while only a few hundred bundles advanced.
2. **The stream itself was slow.** A profile of one full shard scan put **88 % of the time in Python's
   `tarfile._Stream.__read`**. The reader passed `bufsize=8 MiB` to `tarfile`, and tarfile copies its whole remaining
   buffer on every read. So each 512-byte tar header copied about 8 MiB, and a shard of many small files streamed at
   only about 80 MB/s.

## What changes

1. **Buffered tar stream (all streaming reads).** The 8 MiB reads move into an `io.BufferedReader` under tarfile,
   and tarfile gets a normal 64 KiB record buffer. The bytes are the same; nothing else changes. This applies to
   `iter_files_from_part` and `iter_spilled_files_from_part`.
2. **Seekable local part cache (`app/services/part_cache.py`).**
   - When a process needs to scan the same part a **second** time, the part is decompressed once into a plain tar on
     the scratch volume (`/scratch/part-cache`), and the data offset of every member is indexed.
   - Every later read is a seek plus a read of exactly that file. A single pass never builds a cache, so it costs
     nothing extra.
   - The cache is shared by all containers that mount the scratch folder.
3. **Scratch mounted where evidence is read.** In `docker-compose.yml` and `services/mobile-extract`, the scratch
   folder is now also mounted in `api`, `worker-parse`, `worker-quality`, `worker-ocr-gpu`, `worker-rag-gpu`,
   `worker-rag-prep`, `worker-report`, `worker-agent` and `worker-progress`. Android/iOS stacks already had it.
4. **Bug fix: helper USB stop.** Before, stopping a USB collection could leave the collection process running when
   the helper had to fall back to killing the process tree. In `host-drive-helper.ps1`, the two taskkill command
   lines lived inside the C# here-string, and PowerShell replaced their `$_` / `$null` with empty text (for example
   `Where-Object { .CommandLine …}`), so that fallback never worked. They are now written `` `$_ `` / `` `$null ``.
   - Verified with the PowerShell 7.4 parser AST: no variables are expanded inside the C# any more, and the C# that
     `Add-Type` receives contains the literal `$_.CommandLine`.
   - It compiles under C# 5 (mcs) and PowerShell 7 `Add-Type`.

## Measured (synthetic shard built like the real ones)

The test shard had 1.6 GB, 12,012 files, a median of about 30 KB plus twelve 50 MB databases, and incompressible
data. It ran in a 2-vCPU container, and results were compared by SHA-256 of every returned file.

| Workload | Before | After | Same bytes |
|---|---|---|---|
| One pass over 3,000 files (disk parse / OCR / media review pattern) | 19.0 s | **1.75 s** (10.8×) | yes |
| 30 random single-file reads (mobile parse / recovery / preview pattern) | 259.9 s (8.7 s each) | **8.1 s**, including a one-time 4.7 s cache build | yes |
| Random read after the cache exists | 8.7 s | **0.1 ms** | yes |
| Random read with `EXTRACT_PART_CACHE=off` (buffer fix only) | 8.7 s | 0.52 s | yes |

The cost of a random read grows with shard size. Before, it was proportional to the shard; with the cache it is
proportional to the file, so the gain is larger on the 18 GB iPhone shards. These are synthetic numbers. Speed on your
live case depends on the CPU, the drive under `scratch`, and MinIO. Check it with the logs below.

## Accuracy and forensic safety
- **Stored evidence is untouched.** The MinIO parts, manifests and hashes are never modified. The cache is a
  disposable working copy; deleting `backup\forensic-data\scratch\part-cache` at any time is safe.
- **Identical bytes.** The cached tar is the decompressed part byte-for-byte. Every read follows the same rules as
  before:
  - order;
  - per-file caps;
  - repeated paths in member order;
  - full-file **size + SHA-256 verification** when the caller asks (mobile direct reads, prefetch);
  - missing files reported as missing.
- **Never stale.** A cached part is used only while the stored object's size and ETag/last-modified still match. The
  store is asked on every read, so a re-extracted part is rebuilt and never served from the old copy.
- **Never worse than before.** If a build cannot complete, it is discarded and the original streaming read is used.
  That covers:
  - a decompression or tar error;
  - a truncated part;
  - not enough space;
  - a stop/pause request, which is re-raised and never swallowed.
- **Disk.**
  - The default budget is the smaller of 25 % of the disk and 50 % of the space above a 40 GB floor.
  - Least-recently-used entries are evicted, and entries unused for 12 h are removed.
  - **Extraction always wins:** before a shard is spooled, cached copies are released until the shard and its upload
    fit above the floor.

## Settings (`.env`, all optional)
```
EXTRACT_PART_CACHE=auto          # auto | always | off
EXTRACT_PART_CACHE_MIN_FREE_GB=40
EXTRACT_PART_CACHE_MAX_IDLE_HOURS=12
# EXTRACT_PART_CACHE_GB=80        # fixed budget instead of the automatic one
# EXTRACT_PART_CACHE_DIR=/scratch/part-cache
```
`EXTRACT_PART_CACHE=off` disables the cache and keeps the faster streaming. With `always`, the cache is built on the
first read of a part; this is useful when the scratch folder is on a fast SSD.

## Install
1. Let running stages reach a pause/checkpoint, using Polaron's Pause workflow.
2. Recreate the containers so the new `/scratch` mounts and code take effect, for example with
   `script_docker\start_docker_local.cmd`. Backend code is bind-mounted, so no image rebuild is needed for Python.
3. Restart the HostDrive helper on the server for the `$_` fix: run `scripts\ensure-host-drive-helper.ps1`. The helper is
   now **5.59**, and the watchdog restarts an older one by itself.
4. Optional: point `EXTRACT_SCRATCH_HOST_DIR` at an SSD/NVMe folder with room for two or three shards.
5. Resume the same jobs. Nothing is re-extracted.

## How to see it working on a live case
- Worker logs show `part-cache built s3://…/part-0000N.tar.zst: N members, X GB in Ys — later reads seek directly`.
- Mobile parse logs (`mobile-parse-perf … prefetch_s=… parse_s=…`): `prefetch_s` should stop dominating.
- Stage cards show more committed bundles per minute over equal 10–20 minute windows.

## Verification
- `backend/tests/test_part_cache_v1_28.py`: **12 tests**, covering:
  - cached vs streaming reads identical across 13 query patterns (caps, repeats, missing files, directories,
    non-ASCII names, correct and wrong SHA-256, wrong size, empty files);
  - spilled reads identical;
  - the new buffered stream identical to the **V1.27 reader** (loaded from the recovery snapshot);
  - auto mode builds on the second scan and never streams again;
  - re-extracted part never served stale;
  - budget and free-space fallbacks;
  - damaged and truncated parts behave as before, and truncated parts are never cached;
  - LRU eviction, idle removal, and space released to extraction;
  - stop requests propagate;
  - mobile direct reads still SHA-256-verified.
- `test_disk_parse_priority_v20261017.py`: its two isolated-function tests now also supply the two new helpers, with
  the cache off so they still test streaming. All 6 pass.
- Related suites (13 files, 125 tests): the same 11 tests fail with and without this release. They depend on the
  host CPU count or on functions that no longer exist (`iter_files_from_part_budgets`). There are no new failures.
- V1.27 and V1.21 client tests still pass (`test_client_mobile_import_v1_27.py`, `test_client_evidence_v1_21.py`).
- `docker-compose.yml` and `services/mobile-extract/docker-compose.yml` parse as YAML, with no duplicate mount
  targets.

## Not verified here
- Throughput on your server: the real MinIO, and the Docker Desktop bind mount for `scratch` on D:.
- The speed-up on the live 179k-bundle iPhone case.
- GPU stages (OCR models, embeddings) are compute-bound. They are not changed beyond faster input reads, because
  changing models or batch limits could change results.
