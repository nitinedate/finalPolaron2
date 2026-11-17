import { describe, expect, it } from "vitest";
import { deviceMatchesPlatform, devicePlatform, noPhoneText } from "./phonePlatform";

describe("V1.31.13: each acquisition page lists only its own phones", () => {
  const iphone = { adapter: "ios_lockdown", os_family: "ios" };
  const android = { adapter: "android_mtp", os_family: "android" };
  const needsAction = { adapter: "android_usb", os_family: "android" };
  const noFamily = { adapter: "android_adb" };

  it("an iPhone is not listed on the Android page, and the other way round", () => {
    expect(deviceMatchesPlatform(iphone, "android")).toBe(false);
    expect(deviceMatchesPlatform(android, "ios")).toBe(false);
    expect(deviceMatchesPlatform(iphone, "ios")).toBe(true);
    expect(deviceMatchesPlatform(android, "android")).toBe(true);
    expect(deviceMatchesPlatform(needsAction, "android")).toBe(true);
  });

  it("uses the adapter when the OS family is missing, and drops unknown devices from fixed pages", () => {
    expect(devicePlatform(noFamily)).toBe("android");
    expect(deviceMatchesPlatform(noFamily, "android")).toBe(true);
    expect(deviceMatchesPlatform({ adapter: "usb_disk" }, "android")).toBe(false);
    expect(deviceMatchesPlatform({ adapter: "usb_disk" }, null)).toBe(true);
  });

  it("says plainly that nothing is connected", () => {
    expect(noPhoneText("android")).toMatch(/^No Android phone is connected/);
    expect(noPhoneText("ios")).toMatch(/^No iPhone or iPad is connected/);
    expect(noPhoneText(null)).toMatch(/^No phone is connected/);
  });
});
