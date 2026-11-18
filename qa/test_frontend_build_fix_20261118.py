"""Regressions for the Polaron mobile frontend build typing fix."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


class MobileFrontendBuildFixTests(unittest.TestCase):
    def test_device_label_declared_in_shared_acquisition_payload(self):
        source = (FRONTEND / "src/lib/types/forensic.ts").read_text(encoding="utf-8")
        header = source.split("export interface AcquisitionStartPayload {", 1)[1].split("export interface AcquisitionProgress", 1)[0]
        self.assertIn("device_label?: string;", header)
        self.assertIn("server_transfer?:", header)

    def test_host_acquisition_accepts_shared_payload_contract(self):
        source = (FRONTEND / "src/lib/hostDriveHelper.ts").read_text(encoding="utf-8")
        self.assertIn('import type { AcquisitionStartPayload } from "./types/forensic";', source)
        signature = source.split("export async function startHostAcquisitionRun(", 1)[1].split("):", 1)[0]
        self.assertIn("payload: AcquisitionStartPayload", signature)
        self.assertNotIn("payload: Record<string, unknown>", signature)

    def test_production_tsconfig_excludes_only_test_sources(self):
        cfg = json.loads((FRONTEND / "tsconfig.json").read_text(encoding="utf-8"))
        excluded = set(cfg.get("exclude") or [])
        self.assertEqual(excluded, {
            "src/**/*.test.ts", "src/**/*.test.tsx",
            "src/**/*.spec.ts", "src/**/*.spec.tsx",
        })
        self.assertEqual(cfg.get("compilerOptions", {}).get("strict"), True)
        self.assertIn("src", cfg["include"])
        self.assertTrue((FRONTEND / "src/pages/forensic/MobileAcquisitionPage.tsx").exists())

    @unittest.skipUnless(shutil.which("tsc"), "TypeScript CLI is not available; run after npm ci")
    def test_strict_typescript_contract_compiles(self):
        proc = subprocess.run(
            ["tsc", "-p", str(ROOT / "qa/tsconfig.mobile-acquisition-20261118.json"), "--pretty", "false"],
            cwd=ROOT, text=True, capture_output=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
