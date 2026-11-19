import { describe, expect, it } from "vitest";

import { DEVICE_LIST_STALE_MS, deviceListFreshnessWarnings } from "./hostDriveHelper";

describe("this computer's phone list freshness (V1.31.6)", () => {
  const now = Date.parse("2026-11-16T17:30:00Z");

  it("says nothing for a list read moments ago", () => {
    expect(deviceListFreshnessWarnings({ scanned_utc: "2026-11-16T17:29:50Z" }, now)).toEqual([]);
  });

  it("says when the list is old and how to restart the helper", () => {
    const [w] = deviceListFreshnessWarnings({ scanned_utc: new Date(now - 33 * 60_000).toISOString() }, now);
    expect(w).toMatch(/last read at .* \(33 min ago\) and is not updating, so its phones are not shown/);
    expect(w).toMatch(/an unplugged one may still be listed/);
    expect(w).toContain("scripts\\ensure-host-drive-helper.ps1 -ForceRestart");
    expect(w).toContain("not while a collection is running");
  });

  it("passes on why the list was read without re-scanning USB", () => {
    const note = "A phone copy is running on this computer (iPhone backup); phones are listed from Windows without re-scanning USB.";
    expect(deviceListFreshnessWarnings({ scanned_utc: new Date(now - 5_000).toISOString(), note }, now)).toEqual([note]);
  });

  it("stays quiet for an older helper that does not report when it read the list", () => {
    expect(deviceListFreshnessWarnings({}, now)).toEqual([]);
    expect(DEVICE_LIST_STALE_MS).toBe(120_000);
  });
});
