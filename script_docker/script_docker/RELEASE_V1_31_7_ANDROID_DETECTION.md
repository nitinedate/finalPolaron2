# Polaron V1.31.7: the Android page finds the Android phone on the client computer

## What was reported
On Android acquisition (on NITIN, the client computer), with an Android phone plugged in:
- the page still showed only **"Apple iPhone · this PC"** under "A phone is connected, and it is not an Android
  device";
- the V1.31.6 hint ("No Android phone is detected yet ...") was shown, so the website itself was up to date;
- the Android phone was not listed.

## Cause
The page shows this computer's phones from the Polaron helper running on it (`host-drive-helper.ps1`). That helper
listed an Android phone only in two cases:
1. Windows showed it as a portable device **and** its name contained a phone word (Samsung, Galaxy, Redmi,
   Pixel, ...) or its USB vendor ID was on a fixed list. Many phones show only a model code (for example SM-A546E,
   CPH2451 or V2250), and several makers were missing from the list. Such a phone was dropped even with File
   transfer on.
2. USB debugging was on and Windows had a working driver for the debugging interface.

Every other state was silently ignored:
- **charging only**, the default when an Android phone is plugged in;
- File transfer selected, but no Windows portable-device entry for it;
- **no Windows driver** for the phone's USB mode (a yellow warning in Device Manager).

In each of these the phone was on the cable but did not appear anywhere, and the page could not say why.

A second possible cause: **the helper on NITIN was not restarted** after the V1.31.6 update. It keeps running
until it is restarted, and the page could not tell that it was old.

## What changes
- **Found by its File transfer interface.** The helper reads what Windows shows on USB (`pnputil`, or the registry
  during a copy). Any phone with a running File transfer (MTP) interface is listed, whatever its name or maker.
  This also covers a phone that is missing from Windows' portable-device list. The collection finds it by its USB
  vendor and product ID.
  - Cameras (picture transfer without the Android MTP marker), USB modems, SSDs and card readers are not taken
    for phones.
  - Huawei, ZTE and HTC also make USB modems or headsets, so they need phone words, like Samsung and Sony.
- **Plugged in but not usable yet: listed, with what to do.** A phone on charging only, or with no Windows driver
  for its USB mode, now appears on the Android page as an amber card that cannot be selected yet:
  - *Connected for charging only*: unlock the phone, open the USB notification and choose File transfer (for a
    deeper collection also turn on USB debugging and tap Allow), then Rescan.
  - *No Windows driver for its USB mode*: choose File transfer on the phone (no extra driver needed), or install
    the phone maker's USB driver, then Rescan.

  A toast says the same once. Malware Detection shows such a phone as not ready, with the same advice.
- **Old helper flagged.** When the helper on this computer is older than 5.62, the page says so under "Detection
  limitations" and how to restart it.
- **Diagnostic.** `scripts\diagnose-phone-detection.ps1` (new, read-only) writes
  `%TEMP%\polaron-phone-detection.txt`. It starts with a "WHAT TO DO" summary, then lists:
  - the running helper's version and folder;
  - the phone list the helper serves;
  - the USB devices Windows shows, and what the helper makes of each Android phone;
  - `adb devices -l`;
  - the portable-device entries;
  - collection processes still running.

  It does not stop, change or reset anything.
- Helper **5.62**. `ensure-host-drive-helper.ps1` expects 5.62, so it restarts an older helper by itself. Both
  copies of the phone scan (startup and background refresh) use the same new code.

## What you do
1. Unzip `POLARON_V1_31_7_UPDATED_FILES.zip` over the Polaron folder on the **server** (deploy as usual) **and** on
   **NITIN**. It contains V1.31.0 to V1.31.7. On NITIN, use the folder the helper runs from (the diagnostic
   shows it).
2. On NITIN, with no collection running:
   `powershell -ExecutionPolicy Bypass -File scripts\ensure-host-drive-helper.ps1 -ForceRestart`
3. Plug in the Android phone **with a data cable**, unlock it, and on the phone choose **USB: File transfer** (for a
   deeper collection also turn on USB debugging and tap Allow). Unplug the iPhone if it is not part of this
   collection. Click **Rescan** on the Android page.
4. If the phone is still not listed, run on NITIN:
   `powershell -ExecutionPolicy Bypass -File scripts\diagnose-phone-detection.ps1`
   and send `%TEMP%\polaron-phone-detection.txt`.

Restarting the helper stops any collection that is still running, so do it only when none is.

## Tests
- `tests/device_list_refresh_checks.ps1`, 29 checks (15 new). The helper's real refresh job runs against a
  simulated Windows. New cases:
  - with an iPhone plugged in, a charging-only Android phone is listed as needing action, with guidance;
  - a File transfer phone with a model-code name and an unlisted vendor is listed, with and without a
    portable-device entry;
  - an SSD, a USB modem and a camera are not listed;
  - from `pnputil` output: a debugging interface without a driver gives "no driver"; a Samsung in File transfer
    mode is ready and named by its model; a Samsung on charging only needs action;
  - both copies of the new code are identical and wired into both scans.
- `tests/phone_detection_diagnose_checks.ps1`, 8 checks (new): the diagnostic against a stand-in old helper in
  another folder and a saved `pnputil` output. It reports the old version, the other folder and the charging-only
  phone, skips hubs, is ASCII and changes nothing.
- `backend/tests/test_android_usb_detection_v1_31_7.py`, 6 tests (new): the helper-list fallback, the platform
  filter, Malware Detection "not ready", and the helper source.
- `frontend/src/lib/hostDriveHelper.androidUsb.test.tsx`, 8 tests (new): needs-action mapping, the version
  compare and warning, and the warning in this computer's scan.
- Frontend: `tsc -b` clean, vitest 78/78, `vite build` OK.
- Backend full suite: no new failures. It has the same 228 earlier failures and collection errors as the original
  files, which are unrelated to this change.
- All earlier offline suites still pass: Caddy 90, server drive hot-plug 11, iOS stall 15, iOS recovery 10.
- Not run on Windows or with a real phone here. Windows' USB output is simulated from its documented format.

During testing, a bug in the first draft of the new USB reader was found and fixed: it misread every device after
the first.

Originals: `recovery-snapshots/pre-v1.31.7-android-detect-20261116/`.
