# Polaron frontend revision mismatch — repair guide (18 November 2026)

## Verified cause in the available release

The current Windows Docker build references:

- `frontend/src/components/forensic/ClientSetupPanel.tsx`
- `frontend/src/lib/clientMobileImport.ts`

Neither exists in the **complete 17/18-November source ZIP previously delivered**. They have been introduced by another revision or left on disk after unzipping one version over another. These new files call APIs that do not exist in that delivered revision, hence the TS2305/TS2339 errors. An npm upgrade, Docker/HTTPS configuration change, or permissive `any` casts would not repair this source mismatch.

The previous `tsconfig.json` enumerated all of `src`; thus even unreferenced leftover files are typechecked. This release changes the production `tsconfig.json` to compile `src/main.tsx` and its entire imported graph (plus `src/vite-env.d.ts`). It **does not switch off strict checks** for code the app actually imports. Unreferenced leftover modules cannot break production builds. Imported modules, including dynamic `import()`, are still compiled and typechecked.

## Option A — first apply only the non-destructive build fix

Back up the project. Extract `Polaron_Frontend_Revision_Repair_Patch_20261118.zip` into the **project root** (e.g., `D:\project1\polaron`), preserving folders. This updates only `frontend/tsconfig.json`, the source-version repair script, and documentation/tests.

From the project root:

```powershell
# Show currently reachable project files; old orphan files are excluded
cd frontend
npx tsc -b --pretty false
npm run build:unified
cd ..
docker compose -f docker-compose.https.yml build frontend
docker compose -f docker-compose.https.yml up -d frontend
```

The original React files remain unchanged. If the two incompatible files are not imported by the live app, this fixes the build. If they ARE imported, TypeScript correctly continues reporting missing exports. Do not add stub APIs that return fake values; the new import feature needs its matching backend and precise typed contracts.

## Option B — reproducible clean frontend replacement (if A still fails)

**Warning:** This intentionally restores the frontend from the known complete release. The frontend currently on your PC may contain newer custom features, and they will not be present in the restored frontend. The script preserves the full previous frontend source and relevant configs in a backup directory outside the Docker build context, so they can be merged back after identifying their matching version. It never modifies PostgreSQL, Redis, evidence images, or backend storage.

1. Download `Polaron_Frontend_Revision_Repair_Complete_20261118.zip`.
2. Keep it in a known location, e.g., `D:\Downloads\Polaron_Frontend_Revision_Repair_Complete_20261118.zip`.
3. From the existing project directory, first inspect what would be replaced:

```powershell
cd D:\project1\polaron
.\scripts\Repair-FrontendSource.ps1 -ProjectRoot . -ReleaseZip 'D:\Downloads\Polaron_Frontend_Revision_Repair_Complete_20261118.zip'
```

4. Review the `NOT IN RELEASE` entries. To explicitly restore the matching source (backed up before replacement):

```powershell
.\scripts\Repair-FrontendSource.ps1 -ProjectRoot . -ReleaseZip 'D:\Downloads\Polaron_Frontend_Revision_Repair_Complete_20261118.zip' -Apply

docker compose -f docker-compose.https.yml build frontend
docker compose -f docker-compose.https.yml up -d frontend
```

The backup path is printed. No other frontend files are silently deleted.

## If the newer client-import files are intentional

The previous complete ZIP does **not** contain the implementations or backend contract for `ClientSetupPanel` / `clientMobileImport`. To keep their functionality, the **actual newer source tree** (including `frontend/src/lib/clientMobileImport.ts`, `frontend/src/components/forensic/ClientSetupPanel.tsx`, `frontend/src/lib/forensicApi.ts`, `frontend/src/lib/hostDriveHelper.ts`, `frontend/src/lib/clientEvidenceAgent.ts`, `frontend/src/lib/types/forensic.ts`, and related backend routers) must be patched as one version. The current release does not claim to restore unreviewed functionality by fabricating endpoints. Option A should be tried first because it is non-destructive.

## Validation scope

- Production TypeScript input graph for this release includes `App.tsx`, `MobileAcquisitionPage.tsx` and `hostDriveHelper.ts`.
- Production TS excludes truly unreachable stale files, but follows normal and lazy imports.
- A test suite checks TypeScript compilation-file graph behavior and the source archive version manifest.
- Full `npm ci`/Vite cannot be run in this environment because the npm registry cannot be resolved; do the full Docker build on the Windows host.
