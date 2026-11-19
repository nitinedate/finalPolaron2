# Polaron V1.31.6: the phone list shows the phone that is actually plugged in

## What was reported
On Android acquisition (NITIN, the examiner's computer), with an Android phone plugged in:
- the page listed **"Apple iPhone · this PC"** under "A phone is connected, and it is not an Android device";
- the Android phone was not listed at all.

## Cause
The page gets this computer's phones from the workstation helper (`host-drive-helper.ps1`, helper 5.60). The
helper does not scan USB on each request. A background job refreshes a cached list every 10-15 s, and the page
reads that cache. The job skipped its refresh entirely in two situations:
1. **Any collection process looked active.** The check matched every process whose command line mentions
   `ios_usbmux_backup`, `native_ios_job`, `host-mobile-acquire` and so on, including leftovers whose collection had
   already been stopped. After the iPhone collections (one hung at 100% earlier), such a leftover kept the cache
   frozen at "Apple iPhone".
2. **Windows' `pnputil` was still busy** from an earlier pass. The pass then produced nothing, and the old list
   stayed.

The cached list carried no time, so the page could not tell that it was old. The unplugged iPhone was shown as
connected, and the Android phone never appeared.

## What changes
- **The list follows the cables during a copy too.** While a real copy runs, `pnputil` is still not started (it can
  reset USB mid-copy). Instead, the phones are read from the registry: each driver's list of the USB devices
  Windows is running, plus their interface IDs, so USB-debugging phones (class FF/42/01) are still recognised.
  Reading the registry causes no USB traffic. The same registry read is used while `pnputil` is busy. A phone
  plugged in or out shows up within about 10-15 s either way.
- **Leftovers do not count.** A collection process whose parent is gone (from a stopped or crashed collection) is no
  longer treated as a copy in progress. Neither are `--verify` checks and `recover-stalled-ios-backup.ps1`.
- **The list says when and how it was read** (`scanned_utc`, `source`, `note`). The page then:
  - warns when this computer's list is more than 2 minutes old: it is not updating, a newly plugged phone is not
    shown, an unplugged one may still be listed, and how to restart the helper;
  - shows why the list was read from the registry (a copy is running, or `pnputil` is busy).
- **Android hint.** When the Android page sees only a phone of another type, it explains how to make an Android
  phone visible: unlock it, use a data cable, choose USB File transfer (or turn on USB debugging and tap Allow),
  then Rescan.
- Helper **5.61**. `ensure-host-drive-helper.ps1` expects 5.61, so it restarts an older running helper by itself.

## What you do (on NITIN, with no collection running)
1. Unzip `POLARON_V1_31_6_UPDATED_FILES.zip` over the Polaron folder (it contains V1.31.0 to V1.31.6; also deploy it
   on the server as usual).
2. Run `powershell -ExecutionPolicy Bypass -File scripts\ensure-host-drive-helper.ps1`. It sees the running 5.60
   helper, restarts it as 5.61, and stops leftover collection processes on the way.
3. Plug in the Android phone, unlock it, choose USB: File transfer (or allow USB debugging), then **Rescan** on the
   Android page. Unplug the iPhone if it is not part of this collection.

Restarting the helper stops any collection that is still running, so do it only when none is.

## Tests
- `tests/device_list_refresh_checks.ps1` (14): the helper's real refresh job, run one pass at a time against a
  simulated Windows registry and process list. Covered:
  - a leftover backup no longer freezes the list;
  - an Android phone with USB debugging is listed, and an Android phone in File transfer mode is listed;
  - an unplugged iPhone is not listed;
  - during a real copy both phones are listed from the registry without `pnputil`, and an iPhone unplugged during
    the copy disappears;
  - a busy `pnputil` no longer freezes the list;
  - `--verify` runs and the recovery script do not pause it.
- `frontend/src/lib/hostDriveHelper.freshness.test.tsx` (4): old-list warning, registry note, older helpers stay
  quiet.
- Helper source-text suites: the same 14 older failures as the original files. The 3 that this change first broke
  are fixed: the expected helper version, and `test_helper_refresh_does_not_reset_usb`, updated to the new function
  and still asserting that `pnputil` is never killed or run during a copy.
- Frontend: `tsc` clean, vitest 70/70, `vite build` OK. All earlier offline suites still pass.
- Not run on Windows here. The registry and process calls are simulated.

Originals: `recovery-snapshots/pre-v1.31.6-phone-list-20261116/`.
