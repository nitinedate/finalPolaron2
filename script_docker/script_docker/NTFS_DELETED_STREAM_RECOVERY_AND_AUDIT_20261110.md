# Deleted NTFS compressed stream read failures — audit and containment

## Observed
The Sleuth Kit raises `ntfs_uncompress_compunit` on several **deleted** OneDrive .odl/.odlgz/.log entries. Invalid phrase-token offsets or shifts mean decompression failed; do **not** assert whether the actual source data was overwritten or corrupted without additional evidence.

## Code change
- Continue scanning after an individual source read failure; never write fabricated/empty recovered files.
- Record **every failure** (path, inode, size hint, classified reason, error) as a compressed JSONL ledger at `jobs/<id>/extracted-disk/parts/read-failures-NNNNN.jsonl.zst` in MinIO.
- Persist its URI in the extraction checkpoint and both partial/final extraction manifests under `read_failures`.
- Preserve a bounded UI log (first ten warnings per shard) to avoid PostgreSQL overload.
- Correct resumption accounting so replacing a completed shard does not double-count skipped files.
- Do not modify the original E01 evidence or automatically claim damaged deleted streams have been recovered.

## Deploy
Apply patch files to matching paths under `D:\project1\polaran2`. Restart only the extraction worker services using your normal launcher/rebuild workflow (not API alone). Avoid stopping an active shard mid-upload; schedule deployment after current extraction completes or pause cleanly first. Old completed shards do not gain historical per-file ledgers; re-extraction of their shard from the original image is required to produce the new ledger. Do not delete E01, MinIO, Postgres or checkpoints.

## Caveat
This is a loss-accounting / robustness fix, **not a decompressor repair**. Recovery of deleted compressed data depends on whether corresponding NTFS content is intact. For exceptional cases use independent read-only forensic tools and preserve separate provenance and hashes. Repeated retrying of the same damaged content is unlikely to help.
