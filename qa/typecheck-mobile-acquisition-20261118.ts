import type { AcquisitionStartPayload } from "../frontend/src/lib/types/forensic";
import { startHostAcquisitionRun } from "../frontend/src/lib/hostDriveHelper";

declare global {
  interface ImportMeta {
    readonly env: { [key: string]: string | undefined };
  }
}

const payload: AcquisitionStartPayload = {
  adapter: "android_mtp",
  device_id: "adb:serial",
  device_label: "Samsung SM-A307FN",
  case_id: "CASE-TEST",
  evidence_id: "E-TEST",
  legal_authority: "test-authority",
  case_root: "/evidence/cases",
  server_transfer: {
    token: "test-token",
    transfer_id: "test-id",
    platform: "android",
    tenant: "platform",
    base_url: "http://127.0.0.1:8081/api/acquisition/transfer",
  },
};
// Compile-time contract: the UI payload must be directly assignable to the host helper.
const result: Promise<{ ok: boolean } | null> = startHostAcquisitionRun(payload).then(x => x ? { ok: x.ok } : null);
void result;
