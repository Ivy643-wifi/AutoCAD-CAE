"""Tests for CAD LLM generation + bounded auto-repair service."""

from __future__ import annotations

import json
from pathlib import Path

from autocae.backend.services.cad_llm_service import (
    CadLLMBuildService,
    CadLLMRepairConfig,
    GeneratedScript,
    ScriptExecutionResult,
)
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


def _make_spec() -> CaseSpec:
    return CaseSpec(
        metadata=CaseSpecMetadata(case_name="llm_flat_plate"),
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


class _FakeProvider:
    def generate_script(  # noqa: PLR0913
        self,
        *,
        spec: CaseSpec,
        attempt: int,
        previous_script: str | None,
        error_context: str | None,
        output_dir: Path,
    ) -> GeneratedScript:
        del spec, previous_script, error_context, output_dir
        return GeneratedScript(
            script_text=f"# fake script attempt={attempt}\nprint('attempt {attempt}')\n",
            provider_meta={"provider": "fake", "attempt": attempt},
        )


class _RetryThenSuccessExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, script_path: Path, output_dir: Path) -> ScriptExecutionResult:
        del script_path
        self.calls += 1
        if self.calls == 1:
            return ScriptExecutionResult(
                success=False,
                return_code=1,
                stdout="",
                stderr="RuntimeError: first attempt failed",
                error_class="runtime_error",
                error_message="RuntimeError: first attempt failed",
            )

        (output_dir / "model.step").write_text("dummy_step", encoding="utf-8")
        (output_dir / "geometry_meta.json").write_text(
            json.dumps(
                {
                    "step_file": str(output_dir / "model.step"),
                    "source": "cadquery",
                    "bounding_box": {
                        "xmin": -100.0,
                        "xmax": 100.0,
                        "ymin": -12.5,
                        "ymax": 12.5,
                        "zmin": -1.0,
                        "zmax": 1.0,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return ScriptExecutionResult(
            success=True,
            return_code=0,
            stdout="ok",
            stderr="",
            error_class="",
            error_message="",
        )


class _NotAllowedFailureExecutor:
    def execute(self, *, script_path: Path, output_dir: Path) -> ScriptExecutionResult:
        del script_path, output_dir
        return ScriptExecutionResult(
            success=False,
            return_code=1,
            stdout="",
            stderr="FileNotFoundError: output path",
            error_class="file_not_found",
            error_message="FileNotFoundError: output path",
        )


def test_cad_llm_retry_then_success_writes_audit(tmp_path: Path) -> None:
    service = CadLLMBuildService(
        provider=_FakeProvider(),
        executor=_RetryThenSuccessExecutor(),
        config=CadLLMRepairConfig(max_attempts=3, repeated_failure_limit=2),
    )
    out = service.build(spec=_make_spec(), output_dir=tmp_path / "run")

    assert out.success is True
    assert out.cad_result is not None
    assert out.audit_path.exists()
    assert out.issue_report_path is None

    audit = json.loads(out.audit_path.read_text(encoding="utf-8"))
    assert audit["status"] == "success"
    assert audit["stop_reason"] == "success"
    assert audit["config"]["max_attempts"] == 3
    assert len(audit["attempts"]) == 2
    assert audit["attempts"][0]["round_result"] == "failed"
    assert audit["attempts"][1]["round_result"] == "success"


def test_cad_llm_stops_when_failure_class_not_allowed(tmp_path: Path) -> None:
    service = CadLLMBuildService(
        provider=_FakeProvider(),
        executor=_NotAllowedFailureExecutor(),
        config=CadLLMRepairConfig(
            max_attempts=4,
            failure_class_filter=("runtime_error",),
            repeated_failure_limit=2,
        ),
    )
    out = service.build(spec=_make_spec(), output_dir=tmp_path / "run")

    assert out.success is False
    assert out.cad_result is None
    assert out.audit_path.exists()
    assert out.issue_report_path is not None and out.issue_report_path.exists()

    audit = json.loads(out.audit_path.read_text(encoding="utf-8"))
    assert audit["status"] == "failed"
    assert audit["stop_reason"] == "failure_class_not_allowed"
    assert len(audit["attempts"]) == 1

    issue = json.loads(out.issue_report_path.read_text(encoding="utf-8"))
    assert issue["error_stage"] == "cad_llm"
    assert issue["stop_reason"] == "failure_class_not_allowed"
