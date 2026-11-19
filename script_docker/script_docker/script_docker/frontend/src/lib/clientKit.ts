/**
 * V1.31.8: the Polaron client files on an examiner computer (the helper on port 9876 and the phone
 * collection scripts) come from the server. The setup file downloaded here brings a computer up to the
 * server's version once; from then on its watchdog keeps it updated by itself.
 */
import { api } from "./api";

export type ClientKitInfo = { ok: boolean; helper_version: string; setup_file?: string };

export const CLIENT_SETUP_FILENAME = "Polaron-Setup-This-Computer.cmd";

let cached: { at: number; info: ClientKitInfo | null } | null = null;

/** The helper version the server hands out (null when the server is older than V1.31.8). */
export async function getClientKitInfo(maxAgeMs = 300_000): Promise<ClientKitInfo | null> {
  if (cached && Date.now() - cached.at < maxAgeMs) return cached.info;
  let info: ClientKitInfo | null = null;
  try {
    const data = await api.get<ClientKitInfo>("/api/client-kit/info", undefined, { timeoutMs: 10_000 });
    info = data && data.helper_version ? data : null;
  } catch {
    info = null;
  }
  cached = { at: Date.now(), info };
  return info;
}

export function resetClientKitCache(): void {
  cached = null;
}

/** Download the one-time setup file for this computer (it names this console's address as its server). */
export async function downloadClientSetup(origin?: string): Promise<void> {
  const consoleOrigin = origin ?? (typeof window === "undefined" ? "" : window.location.origin);
  await api.download("/api/client-kit/setup.cmd", CLIENT_SETUP_FILENAME, { origin: consoleOrigin }, 60_000);
}
