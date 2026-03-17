"""Tests for M1.7 run-level issue_report and index append-only artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from autocae.backend.orchestrator.pipeline import PipelineRunner
from autocae.schemas.case_spec import (
    AnalysisType,
    BoundaryCondition,
    BoundaryType,
    CaseSpec,
    CaseSpecMetadata,
    Geometry,
    GeometryType,
    LayupLayer,
    Load,
    LoadType,
    Material,
    MeshPreferences,
    Topology,
)


def _make_spec(case_id: str = "case_m17_demo") -> CaseSpec:
    return CaseSpec(
        metadata=CaseSpecMetadata(case_id=case_id, case_name="m17_demo"),
        topology=Topology.LAMINATE,
        geometry=Geometry(
            geometry_type=GeometryType.FLAT_PLATE,
            length=200.0,
            width=25.0,
            thickness=2.0,
        ),
        layup=[LayupLayer(angle=a, thickness=0.5) for a in [0.0, 45.0, -45.0, 90.0]],
        materials=[
            Material(
                material_id="default",
                name="Carbon_UD_Default",
                E1=135000.0,
                E2=10000.0,
                G12=5200.0,
                nu12=0.3,
            )
        ],
        loads=[Load(load_type=LoadType.TENSION, magnitude=1000.0, location="LOAD_END")],
        boundary_conditions=[BoundaryCondition(bc_type=BoundaryType.FIXED, location="FIXED_END")],
        analysis_type=AnalysisType.STATIC_TENSION,
        mesh_preferences=MeshPreferences(global_size=2.0),
    )


def test_run_writes_issue_report_and_appends_index(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runner = PipelineRunner(runs_dir=runs_dir, dry_run=True)
    spec = _make_spec()

    # Fail fast at stage 0 without requiring CAD/mesh external runtime.
    runner._validator.validate = lambda s: SimpleNamespace(  # type: ignore[method-assign]
        passed=False,
        errors=["bad spec for test"],
    )

    result_1 = runner.run(spec)
    assert result_1.success is False

    issue_path = result_1.run_dir / "issue_report.json"
    assert issue_path.exists()
    issue = json.loads(issue_path.read_text(encoding="utf-8"))
    assert issue["error_stage"] == "validation"
    assert issue["error_message"]
    assert "root_cause_hint" in issue and issue["root_cause_hint"]
    assert "remediation_hint" in issue and issue["remediation_hint"]

    index_path = runs_dir / "index.jsonl"
    assert index_path.exists()
    lines_1 = [ln for ln in index_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines_1) == 1
    rec_1 = json.loads(lines_1[0])
    assert rec_1["case_id"] == spec.metadata.case_id
    assert rec_1["entry_type"] == "run"
    assert rec_1["error_stage"] == "validation"

    result_2 = runner.run(spec)
    assert result_2.success is False
    lines_2 = [ln for ln in index_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines_2) == 2
