"""Tests for V3 intake service (retrieval-first routing)."""

from __future__ import annotations

import json
from pathlib import Path

from autocae.backend.intake.service import IntakeService
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


def _make_flat_plate_case(case_name: str, case_id: str) -> CaseSpec:
    return CaseSpec(
        metadata=CaseSpecMetadata(case_name=case_name, case_id=case_id),
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
                name="Carbon",
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


def test_intake_reuse_template(tmp_path: Path) -> None:
    svc = IntakeService()
    outcome = svc.intake(
        text="flat plate tension length=210 width=30 thickness=2",
        runs_dir=tmp_path / "runs",
        project_case_library=tmp_path / "project_case_library",
        min_reuse_confidence=0.75,
    )

    assert outcome.case_spec_path.exists()
    assert outcome.intake_decision_path.exists()
    assert outcome.decision["final_path"] == "reuse"
    assert outcome.case_spec.metadata.source == "template_reuse"
    assert outcome.decision["selected_candidate"]["source"] == "template"


def test_intake_generate_when_template_confidence_low(tmp_path: Path) -> None:
    svc = IntakeService()
    outcome = svc.intake(
        text="flat plate thermal analysis",
        runs_dir=tmp_path / "runs",
        project_case_library=tmp_path / "project_case_library",
        min_reuse_confidence=0.75,
    )

    assert outcome.decision["final_path"] == "generate"
    assert outcome.case_spec.metadata.source == "generated_from_intake"
    assert outcome.case_spec.analysis_type == AnalysisType.THERMAL


def test_intake_reuse_project_case_when_high_confidence(tmp_path: Path) -> None:
    lib_root = tmp_path / "project_case_library" / "demo_case"
    lib_root.mkdir(parents=True, exist_ok=True)

    existing = _make_flat_plate_case(case_name="existing_case", case_id="case_existing")
    (lib_root / "case_spec.json").write_text(existing.to_json(), encoding="utf-8")

    svc = IntakeService()
    outcome = svc.intake(
        text="flat plate tension",
        runs_dir=tmp_path / "runs",
        project_case_library=tmp_path / "project_case_library",
        min_reuse_confidence=0.75,
    )

    assert outcome.decision["final_path"] == "reuse"
    assert outcome.case_spec.metadata.source == "project_case_reuse"
    assert outcome.decision["selected_candidate"]["source"] == "project_case"

    decision_data = json.loads(outcome.intake_decision_path.read_text(encoding="utf-8"))
    assert "input_summary" in decision_data
    assert "hit_candidates" in decision_data
    assert decision_data["final_path"] in {"reuse", "generate"}
