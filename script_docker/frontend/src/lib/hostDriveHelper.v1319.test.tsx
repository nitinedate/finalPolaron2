import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", () => ({ api: { get: vi.fn(async () => null) } }));

import {
  deviceNeedsPhoneAction,
  getHostAcquisitionDevices,
  hostMobileToAcquisitionDevice,
  invalidateHelperHealthCache,
  isDeviceListStale,
} from "./hostDriveHelper";
import { clientSetupState } from "../components/forensic/ClientSetupPanel";

afterEach(() => {
  vi.unstubAllGlobals();
  invalidateHelperHealthCache();
});

function stubHelper(version: string, list: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const body = String(url).endsWith("/health")
        ? { ok: true, service: "host-drive-helper", port: 9876, helper_version: version, root: "C:\\Polaron" }
        : list;
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
}

const adbRow = { adapter: "android_adb", device_id: "a1b2c3d4", os_family: "android", label: "ADB Device", status: "adb" };

describe("V1.31.9: only phones that are plugged in now", () => {
  it("does not show a list that stopped updating (the 'ADB Device' with nothing attached)", async () => {
    const twelveMinutesAgo = new Date(Date.now() - 12 * 60_000).toISOString();
    stubHelper("5.63", { ok: true, devices: [adbRow], scanned_utc: twelveMinutesAgo });
    const r = await getHostAcquisitionDevices(2_000);
    expect(r.devices.filter((d) => d.attach_host === "this_pc")).toEqual([]);
    expect(r.local_helper).toMatchObject({ version: "5.63", outdated: false, stale: true });
    expect(r.warnings.some((w) => w.includes("so its phones are not shown"))).toBe(true);
  });

  it("shows a live list (helper 5.64 reads Windows at every request)", async () => {
    stubHelper("5.64", { ok: true, devices: [adbRow], scanned_utc: new Date().toISOString(), source: "live" });
    const r = await getHostAcquisitionDevices(2_000);
    expect(r.devices.map((d) => d.device_id)).toEqual(["a1b2c3d4"]);
    expect(r.local_helper?.stale).toBe(false);
  });

  it("an empty live list means nothing is plugged in", async () => {
    stubHelper("5.64", { ok: true, devices: [], scanned_utc: new Date().toISOString(), source: "live" });
    const r = await getHostAcquisitionDevices(2_000);
    expect(r.devices).toEqual([]);
    expect(r.warnings).toEqual([]);
  });

  it("isDeviceListStale only for a list that says when it was read", () => {
    const now = Date.parse("2026-11-17T02:20:00Z");
    expect(isDeviceListStale({ scanned_utc: "2026-11-17T02:19:00Z" }, now)).toBe(false);
    expect(isDeviceListStale({ scanned_utc: "2026-11-17T02:01:00Z" }, now)).toBe(true);
    expect(isDeviceListStale({}, now)).toBe(false);
  });

  it("a USB-debugging phone from the mobile list merges with its android_adb row (one card, not two)", () => {
    const d = hostMobileToAcquisitionDevice({ id: "a1b2c3d4", name: "Redmi Note 12", connection: "adb", os_hint: "android" });
    expect(`${d.adapter}:${d.device_id}`).toBe(`${adbRow.adapter}:${adbRow.device_id}`);
    expect(d.status).toBe("adb");
    expect(deviceNeedsPhoneAction(d)).toBe(false);
  });

  it("a live iPhone row stays collectable", () => {
    const d = hostMobileToAcquisitionDevice({
      id: "USB\\VID_05AC&PID_12A8\\00008110001A2B3C4D5E801E",
      name: "Apple iPhone",
      connection: "usb",
      os_hint: "ios",
    });
    expect(d.adapter).toBe("ios_lockdown");
    expect(deviceNeedsPhoneAction(d)).toBe(false);
  });

  it("a stale list asks to update this computer", () => {
    expect(
      clientSetupState({
        localHelper: { version: "5.63", outdated: false, stale: true },
        helperOnline: true,
        serverHelperVersion: "5.64",
        officeHost: false,
      }),
    ).toBe("outdated");
  });
});
