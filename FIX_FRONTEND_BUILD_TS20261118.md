# Polaron Mobile — Fix frontend TypeScript build (18 November 2026)

This patch fixes the `npm run build:unified` failures reported when Docker is building the HTTPS gateway frontend. The error is TypeScript (exit 2), **not** HTTPS ports/firewall/DNS. The npm upgrade notice is unrelated.

## Exact corrections

1. `frontend/src/lib/types/forensic.ts` — `AcquisitionStartPayload` now declares `device_label?: string`, which the acquisition page already supplies. This resolves TS2353 without dropping the human-readable model/label from the forensic event.
2. `frontend/src/lib/hostDriveHelper.ts` — `startHostAcquisitionRun(payload: AcquisitionStartPayload)` uses the actual shared acquisition interface instead of `Record<string, unknown>`. This resolves TS2345 while keeping the input strongly typed and avoiding unsafe `as unknown as Record<...>` casts.
3. `frontend/tsconfig.json` — the **production** TypeScript project excludes `.test.ts(x)` and `.spec.ts(x)` files. Unit tests are still run separately using Vitest. This stops the local `hostDriveHelper.v1319.test.tsx` TS18047 nullability diagnostics from aborting a production build; no test is deleted and no global null-checking is disabled.

**Local test-file note:** The source ZIP used as input (`Polaron_Mobile_Server_Transfer_Complete_20261117.zip`) does *not* contain `frontend/src/lib/hostDriveHelper.v1319.test.tsx`. That test appears to be present in your newer Windows working tree. To make the actual two test assertions type-safe as well (rather than just excluding tests from the production build), add an explicit null guard in *each test block*, immediately after `d` is obtained and before the first `d.*` dereference:

```ts
if (d === null) {
  throw new Error("Expected a detected device, but discovery returned null");
}
// TypeScript now knows d is non-null in this block.
expect(d.os_hint).toBe("android"); // example field only; preserve your original assertion
```

Do not use `d!` merely to silence the errors; retain explicit failure on a null discovery result. If the file is not modified, it will still run under Vitest, and the production build will not typecheck it. The original test source is required to issue a line-exact patch for its assertions.

## Apply

**Recommended:** back up your project; extract the **complete fixed project ZIP** over your existing project root. This contains all earlier mobile transfer changes, not only the frontend fix. Alternately extract the **changed files ZIP** over the project root; it changes only the two TypeScript sources, the frontend tsconfig, and adds this document and the focused QA typecheck.

With Windows PowerShell at the project root:

```powershell
cd frontend
npm ci
npx tsc -b --pretty false
npm run build:unified
npm test
cd ..
docker compose -f docker-compose.https.yml build frontend
docker compose -f docker-compose.https.yml up -d frontend
```

When your production startup script manages Compose, you can rerun that same script after verifying the frontend build. Do not change router ports 80/443 for this TypeScript error. `docker compose ... build` uses the corrected `frontend` folder.

## Focused type regression check

From the project root (TypeScript installed from `frontend/npm ci`, or available globally):

```powershell
cd frontend
npx tsc -p ../qa/tsconfig.mobile-acquisition-20261118.json
```

This checks that `AcquisitionStartPayload` can carry `device_label` and `server_transfer` to `startHostAcquisitionRun()` without the TS2345/TS2353 mismatch.

## Validation performed here

- Standalone strict TypeScript compile of the **real** acquisition payload type and host helper: **PASS**.
- `tsc --showConfig`: acquisition page included and `.test.ts(x)` files excluded from production: **PASS**.
- The edited TypeScript files pass TypeScript parser/transpilation checks: **PASS**.
- **Full `npm ci` / Vite build not run successfully in this environment:** registry.npmjs.org DNS resolution failed (`EAI_AGAIN`), so the frontend dependency installation timed out. The user's Docker log shows that their own `npm ci` completed; rerun it there to verify the full build.
- No live USB phones or deployment endpoints were available in this environment.

No changes were made to mobile acquisition logic, evidence transfer contracts, PostgreSQL, RAG or vulnerability scanner services apart from the stated typing changes. Keep the earlier complete mobile transfer package in place.
