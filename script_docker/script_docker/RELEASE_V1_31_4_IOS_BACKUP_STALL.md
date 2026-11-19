# Polaron V1.31.4: an iPhone collection no longer hangs at 65% ("iPhone backup 100%")

## The problem
Mobile acquisition, iPhone, Advanced Logical, collected on the examiner's computer (forensic server elsewhere):
- "Collecting - iPhone backup 100% - 71.89 GB, 105811 files", overall **65%**, stage acquire, for 30+ minutes;
- "Updated" kept changing, so it looked alive.

## Cause
- **The 65% is fixed arithmetic.** Overall progress is 5% + backup% x 0.60, so 65% means the iPhone reported its
  backup as 100%. The monitor kept re-writing that number every 2 s, which is why "Updated" moved.
- **The backup tool can wait forever.** `ios_usbmux_backup.py` runs pymobiledevice3's `Mobilebackup2Service.backup()`.
  pymobiledevice3 sets no read timeout on the device connection (`sock.settimeout(None)`). It loops in
  `dl_loop()` until the iPhone sends its final "backup finished" message, then unlocks the sync lock over an AFC
  connection that has been idle for the whole backup.
- **Nothing above it had a time limit.** If the iPhone stops talking at that point, the process waits forever. That
  happens when the phone locks or sleeps while finishing, or when the USB/usbmux session dies without notice. The
  PowerShell monitor and the collection job waited on that process with no limit either. The later steps (shared
  photos/WhatsApp, evidence ZIP, hashes) therefore never started.

## What changes (collector on the examiner's computer)
- **Stall watchdog.** While the backup runs, the collector tracks activity: new output from the backup tool, or any
  change in the staging folder (`X:\ib\...`). If there is none for **8 min after 100%** (or **25 min** before):
  1. it stops the stuck session;
  2. it checks the backup on disk with `ios_usbmux_backup.py --verify`. The iPhone's `Manifest.db` must have been
     written in this session (the phone sends it after every file), must open as SQLite, and every file it lists
     must be present (and not shorter than recorded, for unencrypted backups);
  3. **if it is complete**, the collection continues with that backup: it is moved into the case image folder,
     then the shared photos/WhatsApp, the ZIP and the hashes follow, as normal;
  4. **if it is not complete**, the attempt ends with `ios_backup_stalled_no_data_from_iphone`. The job retries
     (up to 4 attempts as before), now with **`--resume`**: it continues the earlier attempt's staging folder
     instead of forcing a new full backup, and the free-space check only applies to a fresh folder.
- **The page tells the examiner what is happening.** After about 2 minutes without data, the collection detail
  shows something like "The iPhone has sent nothing for 3 min while finishing the backup. If it is locked or
  asleep, unlock it and keep the screen on. Polaron checks the backup on disk in 5 min."
- **No step waits forever.**
  - The shared photos/WhatsApp (AFC) step stops after 15 silent minutes and keeps what it pulled. The backup is
    already saved by then.
  - Every wait for a stopped process is bounded.
- **Settings.** Windows environment variables for the helper, optional:
  - `IOS_BACKUP_FINALIZE_STALL_MINUTES` (default 8);
  - `IOS_BACKUP_STALL_MINUTES` (default 25);
  - `IOS_AFC_STALL_MINUTES` (default 15).

## The collection that is stuck now
It runs the old code, which is already loaded, so use the recovery script for it. On the computer with the iPhone,
in the Polaron folder, after copying the update:
```
powershell -ExecutionPolicy Bypass -File scripts\recover-stalled-ios-backup.ps1
```
The script works from the stuck backup process's own command line: it finds the staging folder
(`E:\ib\0CBB401C\20261116_180455`) and the case image folder.
- **If the backup is complete:** it links the case image folder to it (no copy), records the location in
  `EXTERNAL_IOS_PAYLOAD.json`, then stops the stuck process. The collection on the page continues by itself
  (shared photos/WhatsApp, ZIP, hashes).
  - Keep `E:\ib\0CBB401C\20261116_180455`: the case links to it.
- **If it is not complete:** it changes nothing and says so. Unlock the iPhone, keep the screen on, wait 5 minutes,
  and run it again. If it stays incomplete, use **Cancel collection** and collect again: the new version resumes by
  itself.

`-CheckOnly` reports without changing anything.

Do not click Cancel collection before running the script. Do not restart the workstation helper (or run the
launcher on that computer) while a collection is running: a helper restart stops running collections.

## What you do
1. Unzip `POLARON_V1_31_4_UPDATED_FILES.zip` over the Polaron folder **on the computer that collects the iPhone**
   (and on the server as usual; it contains V1.31.0 to V1.31.4).
2. For the stuck collection, run the recovery script above.
3. From the next collection on, nothing to do: the collector loads the new scripts for each collection, so no
   helper restart is needed.
4. Recommended while collecting: iPhone Settings > Display & Brightness > Auto-Lock > Never, and the phone on
   power.

## Tests
- `backend/tests/test_ios_backup_stall_v1_31_4.py` (8): the verify check against a real `Manifest.db` with
  NSKeyedArchiver size records:
  - complete;
  - a missing or truncated file;
  - encrypted sizes not compared;
  - a Manifest from an earlier session;
  - start-of-session placeholders;
  - the command line.

  It also covers `--resume` reusing the earlier staging folder with `full=False`, and a first attempt that is still
  full (pymobiledevice3 faked).
- `tests/ios_backup_stall_checks.ps1` (15): the real monitor, with a fake backup process that hangs like
  pymobiledevice3:
  - stuck at 100% with a complete backup: it continues within seconds, commits the backup to the case, and stops
    the stuck process;
  - stuck with an incomplete backup: retryable stall error;
  - a normal backup is unaffected;
  - `--resume` is passed on retry;
  - default limits.
- `tests/ios_recover_stalled_checks.ps1` (10): the recovery script. It reads the stuck process's command line;
  links and then stops when complete; changes nothing when incomplete or with `-CheckOnly`.
- Related backend suites: 62 passed (54 before plus 8 new).
- Not run against a real iPhone here. The recovery script and the watchdog print the actual state on your computer.

Originals: `recovery-snapshots/pre-v1.31.4-ios-backup-stall-20261116/`.
