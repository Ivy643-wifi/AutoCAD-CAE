"""Tests for doctor service (M1.8)."""

from __future__ import annotations

from pathlib import Path

from autocae.backend.services.doctor_service import DoctorService


def test_doctor_service_runs_and_resolves_manifest(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    tools_dir = project_root / "tools"
    runs_dir = project_root / "runs"
    tools_dir.mkdir(parents=True, exist_ok=True)

    artifact = tools_dir / "dummy_tool.bin"
    artifact.write_bytes(b"dummy")
    import hashlib

    sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest = tools_dir / "manifest.yaml"
    manifest.write_text(
        "\n".join(
            [
                'version: "v1"',
                "artifacts:",
                "  - name: dummy_tool",
                "    path: dummy_tool.bin",
                f"    sha256: {sha}",
                "    required: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = DoctorService().run(project_root=project_root, runs_dir=runs_dir)

    assert report.project_root == project_root.resolve()
    assert report.runs_dir == runs_dir.resolve()
    assert report.manifest_path == manifest.resolve()
    assert len(report.checks) >= 5
