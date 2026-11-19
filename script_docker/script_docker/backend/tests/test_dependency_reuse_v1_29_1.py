"""V1.29.1: ordinary launches reuse installed libraries; application builds start FROM dependency images."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SNAP = ROOT / "recovery-snapshots" / "pre-v1.29.1-dependency-reuse-20261115"
DOCKERFILES = {
    "backend/Dockerfile": ("dependencies", "application"),
    "backend/Dockerfile.malware": ("malware-dependencies", "application"),
    "backend/Dockerfile.scanner": ("dependencies", "application"),
    "frontend/Dockerfile": ("dependencies", "build"),
}


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def stage_instructions(text: str, stage: str) -> list[str]:
    """Non-comment instructions from the top to the end of `stage`, continuation lines joined."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if re.match(rf"^\s*FROM\s+\S+\s+AS\s+{re.escape(stage)}\s*$", l, re.I))
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*FROM\s", lines[i], re.I)), len(lines))
    out, cur = [], ""
    for line in lines[:end]:
        if re.match(r"^\s*#", line) or (not line.strip() and not cur):
            continue
        cur += line.rstrip()
        if cur.endswith("\\"):
            cur = cur[:-1]
            continue
        out.append(re.sub(r"\s+", " ", cur).strip())
        cur = ""
    return [i for i in out if not i.startswith("ARG AETHERIS_DEPS_IMAGE=")]


@pytest.mark.parametrize("rel", sorted(DOCKERFILES))
def test_dockerfile_application_stage_can_start_from_a_dependency_image(rel):
    stage, app = DOCKERFILES[rel]
    text = read(rel)
    assert re.search(rf"(?m)^ARG AETHERIS_DEPS_IMAGE={re.escape(stage)}$", text)
    assert re.search(rf"(?m)^FROM \$\{{AETHERIS_DEPS_IMAGE\}} AS {app}$", text)
    # The global ARG comes before the first FROM so it can be used in FROM.
    assert text.index("ARG AETHERIS_DEPS_IMAGE=") < text.index("\nFROM ")


@pytest.mark.parametrize("rel", sorted(DOCKERFILES))
def test_library_recipes_are_unchanged_so_existing_images_are_reused(rel):
    old = SNAP / rel
    if not old.is_file():
        pytest.skip("pre-V1.29.1 snapshot not present")
    stage = DOCKERFILES[rel][0]
    assert stage_instructions(read(rel), stage) == stage_instructions(old.read_text(encoding="utf-8"), stage)


def test_launcher_builds_applications_from_the_dependency_images():
    ps = read("script_docker/start_docker.ps1")
    body = ps.split("function Invoke-BuildProdApplications {", 1)[1].split("function Remove-ForeignComposeContainerNames {", 1)[0]
    for key in ("backend", "frontend", "malware", "scanner"):
        assert f"Ensure-DependencyImage -Spec $specs['{key}'] -Reuse:$useDeps" in body
    assert "AETHERIS_DEPS_IMAGE=$backendDeps" in body
    assert body.count("-DepsImage $frontendDeps") == 3
    assert "-DepsImage $malwareDeps" in body and "-DepsImage $scannerDeps" in body
    assert "Test-LocalImageBuilds" in body
    ensure = ps.split("function Ensure-DependencyImage {", 1)[1].split("function Get-DockerImageLabelValue {", 1)[0]
    assert "Test-DependencyImageAdoptable" in ensure and "Set-DependencyImageLabel" in ensure
    assert "--label" in ensure and "--target" in ensure
    adopt = ps.split("function Test-DependencyImageAdoptable {", 1)[1].split("function Set-DependencyImageLabel {", 1)[0]
    for needle in ("RootFS.Layers", "image', 'history', '--no-trunc'", "sha256sum", "'--pull', 'never'", "'--network', 'none'"):
        assert needle in adopt, needle
    label = ps.split("function Set-DependencyImageLabel {", 1)[1].split("function Ensure-DependencyImage {", 1)[0]
    assert '"FROM $Image`n"' in label and "RUN" not in label  # label only, nothing reinstalled


def test_scripts_stay_windows_powershell_compatible():
    for rel in ("script_docker/start_docker.ps1", "script_docker/start_docker.lib.ps1"):
        data = (ROOT / rel).read_bytes()
        assert all(b < 128 for b in data) and b"\r\n" in data, rel
    for rel in DOCKERFILES:
        assert b"\r\n" not in (ROOT / rel).read_bytes(), rel
