"""Tests for unified `autocae review` command (M1.6)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import autocae.cli as cli


runner = CliRunner()


def test_review_all_skips_mesh_when_cad_not_passed(monkeypatch, tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "case_review"
    run_dir.mkdir(parents=True, exist_ok=True)
    calls: list[str] = []

    class _FakeCadGateService:
        def run_gate(self, **kwargs):  # noqa: ANN003
            calls.append("cad")
            return SimpleNamespace(
                checks=[{"name": "cad_check", "passed": True, "message": ""}],
                decision=kwargs["decision"],
                next_stage_allowed=False,
                preview_png=None,
                transcript_path=run_dir / "review_transcript.json",
            )

    class _FakeMeshGateService:
        def run_gate(self, **kwargs):  # noqa: ANN003
            calls.append("mesh")
            return SimpleNamespace(
                checks=[{"name": "mesh_check", "passed": True, "message": ""}],
                decision=kwargs["decision"],
                next_stage_allowed=True,
                preview_png=None,
                transcript_path=run_dir / "review_transcript.json",
            )

    monkeypatch.setattr(cli, "CadGateService", _FakeCadGateService)
    monkeypatch.setattr(cli, "MeshGateService", _FakeMeshGateService)

    result = runner.invoke(
        cli.app,
        [
            "review",
            str(run_dir),
            "--stage",
            "all",
            "--cad-decision",
            "edit",
            "--mesh-decision",
            "confirm",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert calls == ["cad"]
    assert "Mesh review skipped" in result.stdout


def test_review_all_runs_cad_and_mesh_when_cad_passed(monkeypatch, tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "case_review"
    run_dir.mkdir(parents=True, exist_ok=True)
    calls: list[str] = []

    class _FakeCadGateService:
        def run_gate(self, **kwargs):  # noqa: ANN003
            calls.append("cad")
            return SimpleNamespace(
                checks=[{"name": "cad_check", "passed": True, "message": ""}],
                decision=kwargs["decision"],
                next_stage_allowed=True,
                preview_png=None,
                transcript_path=run_dir / "review_transcript.json",
            )

    class _FakeMeshGateService:
        def run_gate(self, **kwargs):  # noqa: ANN003
            calls.append("mesh")
            return SimpleNamespace(
                checks=[{"name": "mesh_check", "passed": True, "message": ""}],
                decision=kwargs["decision"],
                next_stage_allowed=True,
                preview_png=None,
                transcript_path=run_dir / "review_transcript.json",
            )

    monkeypatch.setattr(cli, "CadGateService", _FakeCadGateService)
    monkeypatch.setattr(cli, "MeshGateService", _FakeMeshGateService)

    result = runner.invoke(
        cli.app,
        [
            "review",
            str(run_dir),
            "--stage",
            "all",
            "--cad-decision",
            "confirm",
            "--mesh-decision",
            "confirm",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert calls == ["cad", "mesh"]
