# Browser evidence share: end-to-end test driver (V1.31.0)

`driver.mjs` plays the examiner's browser tab. It loads the real
`frontend/src/lib/browserEvidenceShare.ts` (bundled) in Node 22, which has the same
`File`, `Blob`, WebCrypto, `CompressionStream` and `fetch` APIs as the browser, and
serves a folder to the server through the outbound relay.

Run from the project root (needs Node 22+ and a scratch Redis):

```bash
cd frontend && npx esbuild src/lib/browserEvidenceShare.ts --bundle --format=esm \
  --platform=neutral --outfile=../backend/tests/browser_share_e2e/bundle.mjs && cd ..
redis-server --port 6390 --save '' --appendonly no --daemonize yes
cd backend
CLIENT_EVIDENCE_TEST_REDIS_URL=redis://127.0.0.1:6390/0 \
BROWSER_SHARE_E2E_DRIVER=$PWD/tests/browser_share_e2e/driver.mjs \
python -m pytest -q tests/test_browser_evidence_share_v1_31.py
```

Without those two variables the end-to-end test is skipped; the three unit tests still run.
`bundle.mjs` is a build output: rebuild it after changing `browserEvidenceShare.ts`.
