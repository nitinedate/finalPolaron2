# Polaron V1.31.13: each page lists only its own phones, and only while they are plugged in

## What was reported
- With an iPhone connected, the **Android** acquisition page showed it: *"A phone is connected, and it is not an
  Android device… Apple iPhone · this PC · Open iOS acquisition"*. An iPhone should not appear on the Android page
  at all.
- With nothing connected the list should be empty, but it was not always.

## Cause
- **By design until now:** a phone of the other kind was listed with a link to its own page. That is removed.
- **Paths that could still show a phone that is no longer plugged in:**
  - **The helper's drive list** (`/drives`) also carried a phone list, taken from the background file. That file
    could be minutes old.
  - **The server passed on helper lists without checking their age**, both the server's own helper and the
    copies used by remote browsers (`/api/hostdrive/acquisition/devices`, `/api/hostdrive/mobile-devices`,
    device detection). Only this computer's list was age-checked (V1.31.9).
  - **When the server's phone list could not be read**, it fell back to the phones the helper reported **when it
    started**, which may have been unplugged hours earlier.
  - **A phone chosen and then unplugged stayed selected** on the Connect step.

## What changes
- **Only the page's own platform** (`frontend/src/lib/phonePlatform.ts`, Mobile acquisition page).
  - The Android page lists Android phones only: ready ones, and ones that need an action on the phone. The iOS
    page lists iPhones and iPads only.
  - A phone of the other kind is not shown, and does not raise a toast.
  - The platform is taken from the phone's OS family, or else from its adapter.
- **"Nothing connected" says so plainly:** *No Android phone is connected…* or *No iPhone or iPad is
  connected…*. Under it is the time of the check and whether this computer's USB devices were read live.
- **An unplugged phone is no longer selected** while you are choosing one. Once identification or collection
  has begun, the selection is kept: an iPhone can reconnect during a backup.
- **Every helper list is current or not shown** (server side):
  - a list read more than 2 minutes ago is dropped with a warning saying how to restart that helper;
  - so is a list that does not say when it was read (helpers older than 5.61);
  - an older helper's mobile list is judged by the read time of its device list;
  - the fallback to the phones seen at helper start is removed.
- **Helper 5.65:** the phones in `/drives` are read live as well (V1.31.9 did this for the two phone lists). The
  server ignores phones in `/drives` from helpers older than 5.65.
- A USB-debugging phone from the server's mobile list is `android_adb` there too, so it is shown once.

## What you do
1. Deploy `POLARON_V1_31_13_UPDATED_FILES.zip` on the server as usual (website, API, scripts). It contains V1.31.0
   to V1.31.13.
2. Update the helper to 5.65:
   - **Client computers set up with the setup file** update themselves within about 10 minutes, once no
     collection is running. To do it at once, click **Update this computer** on the acquisition page.
   - **The server's own helper** is restarted on the new version by the launcher.
3. Check:
   - with nothing plugged in, both pages say no phone is connected;
   - plug in the iPhone: it appears on the iOS page within a few seconds and not on the Android page;
   - an Android phone appears on the Android page only.

## Tests
- `frontend/src/lib/phonePlatform.test.tsx` (3, new): an iPhone is not on the Android page and the other way
  round; the adapter is used when the OS family is missing; the "nothing connected" text.
- `backend/tests/test_phone_lists_current_v1_31_13.py` (7, new):
  - age rules, including the 7-digit .NET time format;
  - a frozen server list is dropped and a fresh one shown;
  - the fallback trusts only a live mobile list, and maps USB debugging to `android_adb`;
  - the office device list is age-checked;
  - **no startup fallback** when the office phone list cannot be read;
  - an older helper's mobile list is judged by its device list's age;
  - phones in `/drives` are used only from helper 5.65.
- Updated to the new rule (lists carry their read time): `test_hostdrive_agent_refresh.py` (2) and
  `test_android_usb_detection_v1_31_7.py` (2). Version checks moved to 5.65.
- Frontend: `tsc -b` clean, vitest 101/101, `vite build` OK.
- Backend full suite: no new failures. It has the same 228 earlier failures and collection errors as the original
  files.
- PowerShell suites: device list 33, diagnostic 8, client kit 23, Surya 31, drive hot-plug 11.
- The helper's C# compiles with Mono, and its live-scan tests pass.
- Not run on Windows or with a real phone here.

Originals: `recovery-snapshots/pre-v1.31.13-platform-only-live-20261117/`.
