"""Tests for Mesh Gate service and gate guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocae.backend.review.gate_guard import MeshGateError, ensure_mesh_gate_passed
from autocae.backend.review.mesh_gate import MeshGateService


class _FakeMeshVisualizationService:
    def visualize_mesh(  # noqa: PLR0913
        self,
        mesh_inp_file: Path,
        groups_json: Path | None = None,
        output_dir: Path | None = None,
        interactive: bool = False,
        save_png: bool = True,
    ) -> Path | None:
        del mesh_inp_file, groups_json, interactive, save_png
        assert output_dir is not None
        out = output_dir / "viz_mesh.png"
        out.write_bytes(b"fake_png")
        return out


def _prepare_mesh_run_dir(tmp_path: Path, quality_pass: bool = True) -> Path:
    run_dir = tmp_path / "runs" / "case_mesh"
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "mesh.inp").write_text("*NODE\n1,0,0,0\n", encoding="utf-8")
    (run_dir / "mesh_groups.json").write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "group_id": "g1",
                        "entity_type": "surface",
                        "gmsh_tag": 1,
                        "mapped_region": "FIXED_END",
                        "solver_set_name": "FIXED_END",
                        "gmsh_entity_tags": [1],
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "mesh_quality_report.json").write_text(
        json.dumps({"overall_pass": quality_pass}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return run_dir


def test_mesh_gate_confirm_allows_next_stage(tmp_path: Path) -> None:
    run_dir = _prepare_mesh_run_dir(tmp_path, quality_pass=True)
    gate = MeshGateService(visualization_service=_FakeMeshVisualizationService())

    outcome = gate.run_gate(run_dir=run_dir, decision="confirm")

    assert outcome.auto_check_passed is True
    assert outcome.next_stage_allowed is True
    assert outcome.preview_png is not None and outcome.preview_png.exists()
    assert outcome.transcript_path is not None and outcome.transcript_path.exists()

    passed_record = ensure_mesh_gate_passed(run_dir)
    assert passed_record["stage"] == "mesh"
    assert passed_record["user_decision"]["decision"] == "confirm"


def test_mesh_gate_confirm_rejected_when_quality_failed(tmp_path: Path) -> None:
    run_dir = _prepare_mesh_run_dir(tmp_path, quality_pass=False)
    gate = MeshGateService(visualization_service=_FakeMeshVisualizationService())

    with pytest.raises(ValueError, match="auto-check did not pass"):
        gate.run_gate(run_dir=run_dir, decision="confirm")


def test_mesh_gate_guard_rejects_missing_transcript(tmp_path: Path) -> None:
    run_dir = _prepare_mesh_run_dir(tmp_path, quality_pass=True)
    with pytest.raises(MeshGateError, match="transcript not found"):
        ensure_mesh_gate_passed(run_dir)


def test_mesh_gate_edit_writes_blocked_record(tmp_path: Path) -> None:
    run_dir = _prepare_mesh_run_dir(tmp_path, quality_pass=True)
    gate = MeshGateService(visualization_service=_FakeMeshVisualizationService())
    outcome = gate.run_gate(run_dir=run_dir, decision="edit", comment="need finer mesh")

    assert outcome.next_stage_allowed is False
    with pytest.raises(MeshGateError, match="not passed"):
        ensure_mesh_gate_passed(run_dir)
