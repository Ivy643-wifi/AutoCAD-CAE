"""Tests for run artifact locator compatibility layer (M1.9)."""

from __future__ import annotations

from pathlib import Path

from autocae.backend.orchestrator.artifact_locator import ArtifactLocator, RUN_CONTRACT_VERSION


def test_locator_resolves_root_first(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "case_demo"
    run_dir.mkdir(parents=True, exist_ok=True)
    root_mesh = run_dir / "mesh.inp"
    legacy_mesh = run_dir / "03_mesh" / "mesh.inp"
    legacy_mesh.parent.mkdir(parents=True, exist_ok=True)
    root_mesh.write_text("*NODE\n", encoding="utf-8")
    legacy_mesh.write_text("*NODE\n", encoding="utf-8")

    loc = ArtifactLocator(run_dir)
    resolved = loc.resolve("mesh_inp", required=True)
    assert resolved == root_mesh


def test_locator_resolves_legacy_when_root_missing(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "case_demo"
    run_dir.mkdir(parents=True, exist_ok=True)
    legacy_step = run_dir / "02_cad" / "model.step"
    legacy_step.parent.mkdir(parents=True, exist_ok=True)
    legacy_step.write_text("dummy", encoding="utf-8")

    loc = ArtifactLocator(run_dir)
    resolved = loc.resolve("step", required=True)
    assert resolved == legacy_step


def test_locator_contract_snapshot_contains_version() -> None:
    snapshot = ArtifactLocator.contract_snapshot()
    assert snapshot["version"] == RUN_CONTRACT_VERSION
    assert "step" in snapshot["artifacts"]
