/**
 * V1.31.13: the Android page lists only Android phones and the iOS page only iPhones / iPads. A phone of the
 * other kind is not shown on the wrong page at all (before, it was listed with a link to its own page).
 */
export type PhonePlatform = "android" | "ios";

/** Which platform a detected phone belongs to: its OS family, else its adapter. */
export function devicePlatform(device: { os_family?: string | null; adapter?: string | null }): PhonePlatform | "other" {
  const family = String(device.os_family || "").toLowerCase();
  if (family.includes("ios")) return "ios";
  if (family.includes("android")) return "android";
  const adapter = String(device.adapter || "").toLowerCase();
  if (adapter.startsWith("ios")) return "ios";
  if (adapter.startsWith("android")) return "android";
  return "other";
}

export function deviceMatchesPlatform(
  device: { os_family?: string | null; adapter?: string | null },
  platform: PhonePlatform | null,
): boolean {
  if (!platform) return true;
  return devicePlatform(device) === platform;
}

/** What the page says when no phone of its kind is connected. */
export function noPhoneText(platform: PhonePlatform | null): string {
  if (platform === "android") {
    return (
      "No Android phone is connected. Plug it in with a data cable, unlock it and on the phone choose USB: " +
      "File transfer (or turn on USB debugging and tap Allow)."
    );
  }
  if (platform === "ios") {
    return "No iPhone or iPad is connected. Plug it in with a data cable, unlock it and tap Trust when asked.";
  }
  return "No phone is connected. Plug it in with a data cable and unlock it; iPhone and Android are recognised automatically.";
}
