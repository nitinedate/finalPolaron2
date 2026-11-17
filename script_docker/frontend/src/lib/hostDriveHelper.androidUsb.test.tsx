import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", () => ({ api: { get: vi.fn(async () => null) } }));

import {
  PHONE_NEEDS_ACTION_ADAPTER,
  REQUIRED_HELPER_VERSION,
  compareHelperVersions,
  deviceNeedsPhoneAction,
  getHostAcquisitionDevices,
  helperVersionWarning,
  hostMobileToAcquisitionDevice,
  invalidateHelperHealthCache,
  phoneActionLabel,
} from "./hostDriveHelper";

describe("Android phones that need something done on them (V1.31.7)", () => {
  it("maps a charging-only phone from the mobile list to the needs-action adapter", () => {
    const d = hostMobileToAcquisitionDevice({
      id: "a1b2c3d4",
      name: "Android phone",
      connection: "usb",
      os_hint: "android",
      instance_id: "USB\\VID_2717&PID_FF48\\a1b2c3d4",
      state: "charging_only",
      guidance: "Connected for charging only. Choose File transfer, then Rescan.",
    });
    expect(d.adapter).toBe(PHONE_NEEDS_ACTION_ADAPTER);
    expect(d.status).toBe("charging_only");
    expect(d.guidance).toContain("File transfer");
    expect(deviceNeedsPhoneAction(d)).toBe(true);
    expect(phoneActionLabel(d)).toBe("Connected for charging only");
  });

  it("leaves File transfer and iPhone entries collectable", () => {
    const mtp = hostMobileToAcquisitionDevice({ id: "w", name: "Galaxy S23", connection: "mtp", os_hint: "android" });
    const ios = hostMobileToAcquisitionDevice({ id: "i", name: "Apple iPhone", connection: "usb", os_hint: "ios", state: "x" });
    expect(mtp.adapter).toBe("android_mtp");
    expect(ios.adapter).toBe("ios_lockdown");
    expect(deviceNeedsPhoneAction(mtp)).toBe(false);
    expect(deviceNeedsPhoneAction(ios)).toBe(false);
    expect(deviceNeedsPhoneAction({ adapter: "android_adb", status: "adb" })).toBe(false);
  });

  it("labels a phone whose USB mode has no Windows driver", () => {
    expect(deviceNeedsPhoneAction({ adapter: "android_usb", status: "driver_missing" })).toBe(true);
    expect(phoneActionLabel({ status: "driver_missing" })).toBe("No Windows driver for its USB mode");
  });
});

describe("helper version on this computer (V1.31.7)", () => {
  it("compares dotted versions numerically", () => {
    expect(compareHelperVersions("5.61", "5.62")).toBe(-1);
    expect(compareHelperVersions("5.62", "5.62")).toBe(0);
    expect(compareHelperVersions("5.100", "5.62")).toBe(1);
    expect(compareHelperVersions("6", "5.62")).toBe(1);
    expect(REQUIRED_HELPER_VERSION).toBe("5.62");
  });

  it("says an older helper's list is not shown and how to update this computer", () => {
    const w = helperVersionWarning({ ok: true, helper_version: "5.61", port: 9876 });
    expect(w).toContain("version 5.61");
    expect(w).toContain("its phone list is not shown");
    expect(w).toContain("Update this computer");
  });

  it("is quiet for a current helper, an offline one, or the short cached online marker", () => {
    expect(helperVersionWarning({ ok: true, helper_version: "5.62", port: 9876 })).toBeNull();
    expect(helperVersionWarning({ ok: true, helper_version: "5.70" })).toBeNull();
    expect(helperVersionWarning(null)).toBeNull();
    expect(helperVersionWarning({ ok: true, service: "host-drive-helper" })).toBeNull();
    expect(helperVersionWarning({ ok: true, port: 9876 })).toContain("an older version");
  });
});

describe("this computer's device scan carries the version warning", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    invalidateHelperHealthCache();
  });

  function stubHelper(version: string) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const body = String(url).endsWith("/health")
          ? { ok: true, service: "host-drive-helper", port: 9876, helper_version: version, root: "C:\\Polaron" }
          : {
              ok: true,
              devices: [
                { adapter: "ios_lockdown", device_id: "i", os_family: "ios", label: "Apple iPhone" },
                {
                  adapter: "android_usb",
                  device_id: "a1b2c3d4",
                  os_family: "android",
                  status: "charging_only",
                  guidance: "Choose File transfer, then Rescan.",
                },
              ],
              scanned_utc: new Date().toISOString(),
            };
        return new Response(JSON.stringify(body), { status: 200 });
      }),
    );
  }

  it("hides an older helper's phones (V1.31.8) and says why", async () => {
    stubHelper("5.60");
    const r = await getHostAcquisitionDevices(2_000);
    expect(r.warnings.some((w) => w.includes("version 5.60"))).toBe(true);
    expect(r.devices.filter((d) => d.attach_host === "this_pc")).toEqual([]);
    expect(r.local_helper).toEqual({ version: "5.60", outdated: true, stale: false });
  });

  it("lists a current helper's phones without the warning", async () => {
    stubHelper("5.62");
    const r = await getHostAcquisitionDevices(2_000);
    expect(r.warnings.filter((w) => w.includes("Polaron helper on this computer"))).toEqual([]);
    expect(r.local_helper).toEqual({ version: "5.62", outdated: false, stale: false });
    const android = r.devices.find((d) => d.os_family === "android");
    expect(android?.guidance).toBe("Choose File transfer, then Rescan.");
    expect(android && deviceNeedsPhoneAction(android)).toBe(true);
  });
});
