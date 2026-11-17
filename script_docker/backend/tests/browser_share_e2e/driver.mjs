// Plays the examiner's browser tab: the real browserEvidenceShare.ts (bundled) serves a folder.
import { openAsBlob, readdirSync, statSync } from "node:fs";
import { basename, join } from "node:path";
const store = new Map();
globalThis.window = {
  sessionStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) },
  addEventListener() {},
};
const { createBrowserShare, browserShareStats, folderFromInputFiles } = await import("./bundle.mjs");
const [, , relayUrl, root] = process.argv;

// Same File objects a folder <input> gives a page (webkitRelativePath "Folder/sub/file").
const list = [];
async function walk(dir, rel) {
  for (const name of readdirSync(dir).sort()) {
    const full = join(dir, name);
    const st = statSync(full);
    const path = rel ? `${rel}/${name}` : name;
    if (st.isDirectory()) { await walk(full, path); continue; }
    const file = new File([await openAsBlob(full)], name, { lastModified: st.mtimeMs });
    Object.defineProperty(file, "webkitRelativePath", { value: `${basename(root)}/${path}` });
    list.push(file);
  }
}
await walk(root, "");
const folder = folderFromInputFiles(list);
const share = await createBrowserShare({ folder, jobId: "job-e2e", relay: { url: relayUrl, service: "forensic" } });
process.stdout.write(JSON.stringify({
  share_id: share.share_id, token: share.token, manifest_sha256: share.manifest_sha256,
  file_count: share.file_count, total_bytes: share.total_bytes, hostname: share.hostname, root: share.root,
  paths: share.files_preview.map((f) => f.path),
}) + "\n");
setInterval(() => {
  const s = browserShareStats(share.share_id);
  process.stderr.write(JSON.stringify(s || { stopped: true }) + "\n");
  if (!s) process.exit(0);
}, 500);
