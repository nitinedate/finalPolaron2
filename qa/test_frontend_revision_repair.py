"""Release-level regressions for mixed source revision frontend repair.

Use: python -m unittest discover -s qa -p test_frontend_revision_repair.py -v
Requires the globally installed tsc command, not local node_modules.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TS = ROOT / "frontend" / "tsconfig.json"
SRC = ROOT / "frontend" / "src"


class FrontendRevisionRepairTests(unittest.TestCase):
    def test_entrypoints_preserve_strict_typechecking(self):
        obj = json.loads(TS.read_text())
        self.assertTrue(obj["compilerOptions"]["strict"])
        self.assertTrue(obj["compilerOptions"]["noUnusedLocals"])
        self.assertEqual(obj["files"], ["src/main.tsx", "src/vite-env.d.ts"])
        self.assertNotIn("src", obj.get("include", []))

    def test_real_project_reachable_pages_remain_compiled(self):
        result = subprocess.run(
            ["tsc", "-p", str(TS), "--listFilesOnly"],
            text=True, capture_output=True, timeout=60, check=True,
        )
        graph = result.stdout.replace("\\", "/")
        for suffix in (
            "/frontend/src/main.tsx",
            "/frontend/src/App.tsx",
            "/frontend/src/lib/hostDriveHelper.ts",
            "/frontend/src/pages/forensic/MobileAcquisitionPage.tsx",
        ):
            self.assertIn(suffix, graph)

    def test_orphaned_stale_modules_excluded_but_imported_modules_still_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "frontend"
            (project / "src" / "lib").mkdir(parents=True)
            (project / "src" / "components" / "forensic").mkdir(parents=True)
            cfg = {
                "compilerOptions": {
                    "target": "ES2020", "module": "ESNext", "noEmit": True,
                    "strict": True, "skipLibCheck": True,
                    "moduleResolution": "bundler",
                },
                "files": ["src/main.ts", "src/vite-env.d.ts"],
            }
            (project / "tsconfig.json").write_text(json.dumps(cfg))
            main = project / "src" / "main.ts"
            main.write_text('import "./used";\n')
            (project / "src" / "used.ts").write_text("export const active = true;\n")
            stale = project / "src" / "lib" / "clientMobileImport.ts"
            stale.write_text("import { missing } from './nonexistent'; export { missing };\n")
            stale2 = project / "src" / "components" / "forensic" / "ClientSetupPanel.tsx"
            stale2.write_text("export const unused = true;\n")
            (project / "src" / "vite-env.d.ts").write_text("export {};\n")
            def graph():
                res = subprocess.run(
                    ["tsc", "-p", str(project), "--listFilesOnly"],
                    capture_output=True, text=True, check=True, timeout=30,
                )
                return res.stdout.replace("\\", "/")
            self.assertIn("/src/used.ts", graph())
            self.assertNotIn("clientMobileImport.ts", graph())
            self.assertNotIn("ClientSetupPanel.tsx", graph())
            main.write_text('import "./used";\nimport "./lib/clientMobileImport";\n')
            self.assertIn("clientMobileImport.ts", graph())

    def test_no_invented_client_import_api_contract(self):
        # The older source has no backend client-import endpoints, so it would
        # be incorrect to add frontend methods just to make stale code compile.
        client = (ROOT / "frontend" / "src" / "lib" / "forensicApi.ts").read_text()
        self.assertNotIn("startClientImport:", client)
        self.assertNotIn("clientImportCapabilities:", client)

    def test_repair_script_preserves_previous_frontend(self):
        script = (ROOT / "scripts" / "Repair-FrontendSource.ps1").read_text()
        self.assertIn("[switch] $Apply", script)
        self.assertIn("Move-Item -LiteralPath $existingSource -Destination (Join-Path $backup 'src')", script)
        self.assertIn("if (-not $Apply)", script)
        self.assertIn("Frontend replacement failed", script)


if __name__ == "__main__":
    unittest.main()
