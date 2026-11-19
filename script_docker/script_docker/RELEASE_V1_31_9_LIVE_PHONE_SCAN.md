# Polaron V1.31.9: every scan asks Windows what is plugged in now

## What was reported
On NITIN (the client computer):
- with **nothing connected**, the iOS page listed two **"ADB Device · Android · this PC"** cards;
- a newly connected **iPhone was not detected**;
- "Detection limitations" said: *This computer's phone list was last read at 07:31 AM (13 min ago) and is not
  updating.*

What was asked: on each scan, check what is connected and read its details; when nothing is connected, the list
must be empty.

## Cause
- **The list was a file, not a scan.** The helper did not look at USB when the page asked. It returned a file
  that a background job rewrote every 10-15 s. On NITIN that job stopped writing right after the helper started
  at 07:31, so every scan returned the 07:31 list. That list held the Android phone with USB debugging that was
  plugged in then, and could never contain the iPhone plugged in later.
- **Likely reason the job stopped:** each refresh pass called a Windows "keep USB awake" function *before*
  writing the file. That function is compiled on first use. If it fails on a computer (for example, blocked by
  security software), every pass fails before the write. The job hid such errors, so nothing showed it.
- **Two cards for one phone.** The page merges two helper lists. In one of them a USB-debugging phone was treated
  as a File transfer device, so the same phone was shown twice. Its Windows interface name, "ADB Device", was
  used as the phone's name.

## What changes
- **Live scan on every request (helper 5.64).** `/acquisition/devices` and `/mobile-devices` now read the USB
  devices Windows has present at that moment, through SetupAPI (the list Device Manager shows):
  - no background job, no pnputil, no WMI, and no USB traffic;
  - a few milliseconds per scan;
  - the result is reused for at most 1.5 s.

  The helper classifies the devices itself:
  - **iPhone / iPad / iPod**: Apple vendor ID with an iOS product ID. Apple keyboards and adapters are not
    listed. The id carries the UDID the collection uses.
  - **Android**: USB debugging (`android_adb`, named by the phone's model where Windows shows one); File transfer
    (`android_mtp`); or charging only / no driver (an "Action needed" card with what to do).
  - **Not phones**: SSDs, USB modems, cameras and hubs are not listed.
  - **Nothing plugged in**: an empty list.

  The answer says `source: "live"` and carries the time of the scan. If Windows cannot be asked, the background
  file is returned instead. `?cached=1` returns it on purpose (the diagnostic shows both).
- **The background refresh cannot freeze any more.**
  - It writes the file *before* the keep-awake call, and a failing keep-awake call is ignored.
  - Its process query has a 20 s limit.
  - Each pass records its result in `%TEMP%\aetheris_device_refresh_status.json`.
  - The helper restarts the refresh when it has stopped, or has not written for 2 minutes.
- **The page never shows a frozen list.**
  - If this computer's list says it is more than 2 minutes old (an older helper whose refresh stopped), its
    phones are **not shown**, and the panel asks to **Update this computer**.
  - A USB-debugging phone appears once, not twice.
  - "ADB Device" is no longer used as a phone name.
- `ensure-host-drive-helper.ps1` expects 5.64. The diagnostic script shows the live list, the background list and
  the refresh status.

## What you do
1. Deploy `POLARON_V1_31_9_UPDATED_FILES.zip` on the **server** as usual (it contains V1.31.0 to V1.31.9).
2. **NITIN:**
   - If NITIN was set up with `Polaron-Setup-This-Computer.cmd` (V1.31.8), it updates itself to helper 5.64
     within about 10 minutes, once no collection is running.
   - To do it now: on the acquisition page click **Update this computer** (shown while the list is frozen) and
     open the downloaded file again.
3. Click **Rescan**:
   - with nothing connected, the list is empty;
   - plug in the iPhone, unlock it and tap Trust: it appears on the iOS page at the next scan (within a few
     seconds);
   - unplug it: it disappears at the next scan.

   An Android phone appears on the Android page in the same way.

## Tests
- `backend/tests/test_live_usb_scan_v1_31_9.py` (11, new). The helper's C# is compiled with Mono and its classifier
  is fed the USB devices Windows would report. Covered:
  - nothing plugged in gives an empty list;
  - an iPhone gives one `ios_lockdown` row whose id carries the UDID;
  - an Apple keyboard is not listed;
  - Android with USB debugging and File transfer gives one `android_adb` row named by its model;
  - "ADB Device" gives "Android phone";
  - a Samsung in File transfer mode gives `android_mtp`;
  - charging only, and no driver, each give an "Action needed" row;
  - an SSD, a USB modem and a camera are not listed;
  - an iPhone plus Android give both, with JSON escaping;
  - a failed Windows read falls back to the background file;
  - the endpoints use the live read.
- `tests/device_list_refresh_checks.ps1`: 33 checks (4 new). A failing keep-awake call no longer blocks the write;
  "ADB Device" is not a name; nothing plugged in gives no phones; the request loop restarts a stopped refresh.
- `frontend/src/lib/hostDriveHelper.v1319.test.tsx` (7, new). A list 12 minutes old is not shown and asks for an
  update; a live list is shown; an empty live list is empty; the USB-debugging phone merges into one card; a live
  iPhone stays collectable.
- Frontend: `tsc -b` clean, vitest 98/98, `vite build` OK.
- Backend full suite: no new failures. It has the same 228 earlier failures and collection errors as the original
  files.
- PowerShell suites: client kit 23, diagnostic 8, Caddy 90, drive hot-plug 11, iOS stall 15, iOS recovery 10.
- Not run on Windows or with a real phone here. The Windows SetupAPI calls are standard, but they were only
  compiled here, not executed.

Originals: `recovery-snapshots/pre-v1.31.9-live-scan-20261117/`.
