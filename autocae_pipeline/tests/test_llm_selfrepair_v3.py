"""验证测试：LLM 生成+自修复主线机制（V3 第一节核心结论第2条）。

覆盖范围：
  T1  错误分类器统一性（classify_failure 含新增 QUALITY_BELOW_THRESHOLD）
  T2  配置别名统一性（MeshLLMRepairConfig is RepairConfig）
  T3  issue_report 格式一致性（CAD/Mesh 均含 repair_history_summary）
  T4  CAD LLM 修复循环：重试后成功，审计文件结构完整
  T5  CAD LLM 修复循环：不允许错误类停止
  T6  CAD LLM 修复循环：重复失败上限停止
  T7  Mesh LLM 修复循环：重试后成功，issue_report 含 repair_history_summary
  T8  Mesh LLM 修复循环：不允许错误类停止
  T9  Pipeline 自动回落：模板未命中时切换 LLM 模式（V3 "未命中再由LLM生成"）
  T10 RuleBasedCadScriptProvider：无 API Key 时离线生成可执行脚本
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 公用 Fixture
# ---------------------------------------------------------------------------

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


def _make_spec(geometry_type: GeometryType = GeometryType.FLAT_PLATE) -> CaseSpec:
    return CaseSpec(
        metadata=CaseSpecMetadata(case_name="v3_llm_test"),
        topology=Topology.LAMINATE,
        geometry=Geometry(
            geometry_type=geometry_type,
            length=200.0,
            width=25.0,
            thickness=2.0,
        ),
        layup=[LayupLayer(angle=0.0, thickness=0.5)],
        materials=[
            Material(
                material_id="m1",
                name="Carbon_UD",
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


# ---------------------------------------------------------------------------
# T1: 错误分类器统一性
# ---------------------------------------------------------------------------

class TestClassifyFailureUnified:
    """T1 — repair_strategy.classify_failure 覆盖全部错误类型。"""

    from autocae.schemas.repair_strategy import classify_failure, ErrorClass

    @pytest.mark.parametrize("log_text,expected", [
        ("SyntaxError: invalid syntax on line 5", "syntax_error"),
        ("ModuleNotFoundError: No module named 'cadquery'", "import_error"),
        ("ImportError: cannot import name 'foo'", "import_error"),
        ("FileNotFoundError: /tmp/model.step not found", "file_not_found"),
        ("GeometryException: shape is null after boolean op", "geometric_invalid"),
        ("Error: invalid geometry in occ kernel", "geometric_invalid"),
        ("mesh quality fail: min_quality=0.05 quality below threshold", "quality_below_threshold"),
        ("quality_below_threshold: element 423 skewness=0.95", "quality_below_threshold"),
        ("skewness exceed limit: 0.98 > 0.90", "quality_below_threshold"),
        ("aspect ratio exceed 5.0 for element 12", "quality_below_threshold"),
        ("RuntimeError: script crashed unexpectedly", "runtime_error"),
        ("unknown generic failure", "runtime_error"),
    ])
    def test_classify_failure_covers_all_classes(self, log_text: str, expected: str) -> None:
        from autocae.schemas.repair_strategy import classify_failure
        result = classify_failure(log_text)
        assert result == expected, (
            f"classify_failure({log_text!r}) = {result!r}, expected {expected!r}"
        )


# ---------------------------------------------------------------------------
# T2: 配置别名统一性
# ---------------------------------------------------------------------------

class TestRepairConfigAlias:
    """T2 — MeshLLMRepairConfig 和 CadLLMRepairConfig 均为 RepairConfig 的类型别名。"""

    def test_mesh_llm_repair_config_is_repair_config(self) -> None:
        from autocae.schemas.repair_strategy import RepairConfig
        from autocae.backend.services.mesh_llm_service import MeshLLMRepairConfig
        assert MeshLLMRepairConfig is RepairConfig, (
            "MeshLLMRepairConfig 应该是 RepairConfig 的类型别名（M2.4 要求）"
        )

    def test_cad_llm_repair_config_is_repair_config(self) -> None:
        from autocae.schemas.repair_strategy import RepairConfig
        from autocae.backend.services.cad_llm_service import CadLLMRepairConfig
        assert CadLLMRepairConfig is RepairConfig, (
            "CadLLMRepairConfig 应该是 RepairConfig 的类型别名（M2.4 要求）"
        )

    def test_default_values_consistent(self) -> None:
        from autocae.schemas.repair_strategy import RepairConfig
        cfg = RepairConfig()
        assert cfg.max_attempts == 3
        assert cfg.repeated_failure_limit == 2
        assert "syntax_error" in cfg.failure_class_filter
        assert "runtime_error" in cfg.failure_class_filter


# ---------------------------------------------------------------------------
# T3: issue_report 格式一致性
# ---------------------------------------------------------------------------

class TestIssueReportFormatConsistency:
    """T3 — CAD/Mesh issue_report 均含 repair_history_summary（build_issue_report 统一格式）。"""

    def test_build_issue_report_has_repair_history_summary(self) -> None:
        from autocae.schemas.repair_strategy import build_issue_report
        attempts = [
            {"attempt": 1, "round_result": "failed", "error_class": "syntax_error",
             "error_message": "line 1: bad syntax"},
            {"attempt": 2, "round_result": "failed", "error_class": "syntax_error",
             "error_message": "line 2: bad syntax"},
        ]
        report = build_issue_report(stage="cad_llm", stop_reason="repeated_failure_limit",
                                    attempts=attempts)
        assert "repair_history_summary" in report, "issue_report 缺少 repair_history_summary 字段"
        rhs = report["repair_history_summary"]
        assert rhs["total_attempts"] == 2
        assert len(rhs["attempt_results"]) == 2

    def test_cad_and_mesh_issue_reports_have_same_keys(self) -> None:
        from autocae.schemas.repair_strategy import build_issue_report
        attempts = [{"attempt": 1, "round_result": "failed", "error_class": "runtime_error",
                     "error_message": "crash"}]
        cad_report = build_issue_report(stage="cad_llm", stop_reason="max_attempts_reached",
                                        attempts=attempts)
        mesh_report = build_issue_report(stage="mesh_llm", stop_reason="max_attempts_reached",
                                         attempts=attempts)
        assert set(cad_report.keys()) == set(mesh_report.keys()), (
            "CAD 和 Mesh issue_report 的字段集不一致"
        )


# ---------------------------------------------------------------------------
# 公用 Mock：CAD LLM 执行器
# ---------------------------------------------------------------------------

from autocae.backend.services.cad_llm_service import (
    CadLLMBuildService,
    CadLLMRepairConfig,
    GeneratedScript as CadGeneratedScript,
    ScriptExecutionResult as CadScriptExecResult,
)


class _CadFakeProvider:
    def generate_script(self, *, spec, attempt, previous_script, error_context, output_dir):
        del spec, previous_script, error_context, output_dir
        return CadGeneratedScript(
            script_text=f"# cad attempt={attempt}\nprint('ok')\n",
            provider_meta={"provider": "fake_cad", "attempt": attempt},
        )


class _CadRetryThenSuccessExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, script_path, output_dir):
        del script_path
        self.calls += 1
        if self.calls == 1:
            return CadScriptExecResult(
                success=False, return_code=1, stdout="",
                stderr="RuntimeError: first attempt, bad code",
                error_class="runtime_error", error_message="first attempt failure",
            )
        (output_dir / "model.step").write_text("STEP_DATA", encoding="utf-8")
        (output_dir / "geometry_meta.json").write_text(
            json.dumps({
                "step_file": str(output_dir / "model.step"),
                "source": "cadquery",
                "bounding_box": {"xmin": -100.0, "xmax": 100.0, "ymin": -12.5,
                                 "ymax": 12.5, "zmin": -1.0, "zmax": 1.0},
            }), encoding="utf-8",
        )
        return CadScriptExecResult(success=True, return_code=0, stdout="ok", stderr="",
                                   error_class="", error_message="")


class _CadAlwaysFailNotAllowedExecutor:
    def execute(self, *, script_path, output_dir):
        del script_path, output_dir
        return CadScriptExecResult(
            success=False, return_code=1, stdout="",
            stderr="FileNotFoundError: path not found",
            error_class="file_not_found", error_message="file not found",
        )


class _CadAlwaysRuntimeErrorExecutor:
    def execute(self, *, script_path, output_dir):
        del script_path, output_dir
        return CadScriptExecResult(
            success=False, return_code=1, stdout="",
            stderr="RuntimeError: always fails",
            error_class="runtime_error", error_message="always fails",
        )


# ---------------------------------------------------------------------------
# T4: CAD LLM 修复循环：重试后成功
# ---------------------------------------------------------------------------

class TestCadLLMRepairLoopSuccess:
    """T4 — CAD LLM 有界修复：首次失败→第2次成功，审计文件结构验证。"""

    def test_retry_then_success_audit_structure(self, tmp_path: Path) -> None:
        service = CadLLMBuildService(
            provider=_CadFakeProvider(),
            executor=_CadRetryThenSuccessExecutor(),
            config=CadLLMRepairConfig(max_attempts=3, repeated_failure_limit=2),
        )
        out = service.build(spec=_make_spec(), output_dir=tmp_path / "run")

        assert out.success is True
        assert out.cad_result is not None
        assert out.issue_report_path is None, "成功时不应生成 issue_report"

        audit = json.loads(out.audit_path.read_text(encoding="utf-8"))
        assert audit["stage"] == "cad_llm"
        assert audit["status"] == "success"
        assert audit["stop_reason"] == "success"
        assert audit["config"]["max_attempts"] == 3
        assert len(audit["attempts"]) == 2
        assert audit["attempts"][0]["round_result"] == "failed"
        assert audit["attempts"][0]["repair_action"] == "initial_generation"
        assert audit["attempts"][1]["round_result"] == "success"
        assert "repair_from_" in audit["attempts"][1]["repair_action"], (
            "第2次修复动作应包含前次错误上下文"
        )

    def test_attempt_dirs_created_per_attempt(self, tmp_path: Path) -> None:
        service = CadLLMBuildService(
            provider=_CadFakeProvider(),
            executor=_CadRetryThenSuccessExecutor(),
            config=CadLLMRepairConfig(max_attempts=3),
        )
        out = service.build(spec=_make_spec(), output_dir=tmp_path / "run")
        llm_dir = tmp_path / "run" / "cad_llm"
        assert (llm_dir / "attempt_01" / "generated_cad.py").exists()
        assert (llm_dir / "attempt_01" / "execution.log").exists()
        assert (llm_dir / "attempt_02" / "generated_cad.py").exists()


# ---------------------------------------------------------------------------
# T5: CAD LLM 修复循环：不允许的错误类
# ---------------------------------------------------------------------------

class TestCadLLMRepairLoopNotAllowed:
    """T5 — CAD LLM：failure_class_filter 阻止不在列表中的错误类重试。"""

    def test_stops_on_not_allowed_error_class(self, tmp_path: Path) -> None:
        service = CadLLMBuildService(
            provider=_CadFakeProvider(),
            executor=_CadAlwaysFailNotAllowedExecutor(),
            config=CadLLMRepairConfig(
                max_attempts=5,
                failure_class_filter=("runtime_error", "syntax_error"),  # file_not_found 不在列表
                repeated_failure_limit=3,
            ),
        )
        out = service.build(spec=_make_spec(), output_dir=tmp_path / "run")

        assert out.success is False
        assert out.issue_report_path is not None and out.issue_report_path.exists()

        audit = json.loads(out.audit_path.read_text(encoding="utf-8"))
        assert audit["stop_reason"] == "failure_class_not_allowed"
        assert len(audit["attempts"]) == 1, "不允许的错误应在第1次就停止"

        issue = json.loads(out.issue_report_path.read_text(encoding="utf-8"))
        assert issue["error_stage"] == "cad_llm"
        assert issue["stop_reason"] == "failure_class_not_allowed"
        assert "repair_history_summary" in issue


# ---------------------------------------------------------------------------
# T6: CAD LLM 修复循环：重复失败上限
# ---------------------------------------------------------------------------

class TestCadLLMRepairLoopRepeatedLimit:
    """T6 — CAD LLM：repeated_failure_limit 限制同类错误重试次数。"""

    def test_stops_on_repeated_failure_limit(self, tmp_path: Path) -> None:
        service = CadLLMBuildService(
            provider=_CadFakeProvider(),
            executor=_CadAlwaysRuntimeErrorExecutor(),
            config=CadLLMRepairConfig(
                max_attempts=5,
                failure_class_filter=("runtime_error",),
                repeated_failure_limit=2,  # 同类错误最多2次
            ),
        )
        out = service.build(spec=_make_spec(), output_dir=tmp_path / "run")

        assert out.success is False
        audit = json.loads(out.audit_path.read_text(encoding="utf-8"))
        assert audit["stop_reason"] == "repeated_failure_limit"
        assert len(audit["attempts"]) == 2, "repeated_failure_limit=2 时应在第2次后停止"


# ---------------------------------------------------------------------------
# 公用 Mock：Mesh LLM 执行器
# ---------------------------------------------------------------------------

from autocae.backend.services.mesh_llm_service import (
    MeshLLMBuildService,
    MeshLLMRepairConfig,
    GeneratedScript as MeshGeneratedScript,
    ScriptExecutionResult as MeshScriptExecResult,
)
from autocae.backend.templates.cad.base import CADResult
from autocae.schemas.mesh import GeometryMeta, GeometrySource


def _make_cad_result(output_dir: Path) -> CADResult:
    (output_dir / "model.step").write_text("STEP_DATA", encoding="utf-8")
    meta = GeometryMeta(
        step_file=str(output_dir / "model.step"),
        source=GeometrySource.CADQUERY,
        bounding_box={"xmin": -100.0, "xmax": 100.0, "ymin": -12.5,
                      "ymax": 12.5, "zmin": -1.0, "zmax": 1.0},
    )
    return CADResult(step_file=output_dir / "model.step", geometry_meta=meta)


class _MeshFakeProvider:
    def generate_script(self, *, spec, cad_result, attempt, previous_script,
                        error_context, output_dir):
        del spec, cad_result, previous_script, error_context, output_dir
        return MeshGeneratedScript(
            script_text=f"# mesh attempt={attempt}\nprint('ok')\n",
            provider_meta={"provider": "fake_mesh", "attempt": attempt},
        )


class _MeshRetryThenSuccessExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, script_path, output_dir):
        del script_path
        self.calls += 1
        if self.calls == 1:
            return MeshScriptExecResult(
                success=False, return_code=1, stdout="",
                stderr="RuntimeError: mesh generation failed",
                error_class="runtime_error", error_message="mesh failed",
            )
        (output_dir / "mesh.inp").write_text("*HEADING\n", encoding="utf-8")
        (output_dir / "mesh_groups.json").write_text(
            json.dumps({
                "geometry_id": "geo_test", "mesh_file": str(output_dir / "mesh.inp"),
                "groups": [{"group_id": "pg_solid", "entity_type": "volume",
                            "gmsh_tag": 1, "mapped_region": "SOLID",
                            "solver_set_name": "SOLID", "gmsh_entity_tags": [1]}],
                "node_count": 10, "element_count": 5,
            }), encoding="utf-8",
        )
        (output_dir / "mesh_quality_report.json").write_text(
            json.dumps({
                "geometry_id": "geo_test", "mesh_file": str(output_dir / "mesh.inp"),
                "element_count": 5, "node_count": 10,
                "min_quality": 0.8, "avg_quality": 0.9,
                "max_aspect_ratio": 1.5, "checks": [], "warnings": [],
                "failed_checks": [], "overall_pass": True,
            }), encoding="utf-8",
        )
        return MeshScriptExecResult(success=True, return_code=0, stdout="ok", stderr="",
                                    error_class="", error_message="")


class _MeshAlwaysFailNotAllowedExecutor:
    def execute(self, *, script_path, output_dir):
        del script_path, output_dir
        return MeshScriptExecResult(
            success=False, return_code=1, stdout="",
            stderr="FileNotFoundError: model.step not found in mesh script",
            error_class="file_not_found", error_message="step file missing",
        )


# ---------------------------------------------------------------------------
# T7: Mesh LLM 修复循环：重试后成功 + issue_report 含 repair_history_summary
# ---------------------------------------------------------------------------

class TestMeshLLMRepairLoopSuccess:
    """T7 — Mesh LLM 修复：重试后成功，issue_report 使用 build_issue_report 统一格式。"""

    def test_retry_then_success_audit_and_format(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        service = MeshLLMBuildService(
            provider=_MeshFakeProvider(),
            executor=_MeshRetryThenSuccessExecutor(),
            config=MeshLLMRepairConfig(max_attempts=3, repeated_failure_limit=2),
        )
        out = service.build(spec=_make_spec(), cad_result=_make_cad_result(run_dir),
                            output_dir=run_dir)

        assert out.success is True
        assert out.mesh_groups is not None
        assert out.mesh_quality is not None
        assert out.issue_report_path is None, "成功时不应生成 issue_report"

        audit = json.loads(out.audit_path.read_text(encoding="utf-8"))
        assert audit["stage"] == "mesh_llm"
        assert audit["status"] == "success"
        assert audit["stop_reason"] == "success"
        assert len(audit["attempts"]) == 2

    def test_fail_issue_report_has_repair_history_summary(self, tmp_path: Path) -> None:
        """失败时 mesh issue_report 必须含 repair_history_summary（M2.4 统一格式）。"""
        run_dir = tmp_path / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        service = MeshLLMBuildService(
            provider=_MeshFakeProvider(),
            executor=_MeshAlwaysFailNotAllowedExecutor(),
            config=MeshLLMRepairConfig(
                max_attempts=3,
                failure_class_filter=("runtime_error",),  # file_not_found 不在列表
            ),
        )
        out = service.build(spec=_make_spec(), cad_result=_make_cad_result(run_dir),
                            output_dir=run_dir)

        assert out.success is False
        assert out.issue_report_path is not None

        issue = json.loads(out.issue_report_path.read_text(encoding="utf-8"))
        assert issue["error_stage"] == "mesh_llm"
        # 核心检查：使用 build_issue_report 后必须含此字段
        assert "repair_history_summary" in issue, (
            "Mesh issue_report 缺少 repair_history_summary（修改2b未生效）"
        )
        rhs = issue["repair_history_summary"]
        assert "total_attempts" in rhs
        assert "attempt_results" in rhs


# ---------------------------------------------------------------------------
# T8: Mesh LLM 修复循环：不允许的错误类
# ---------------------------------------------------------------------------

class TestMeshLLMRepairLoopNotAllowed:
    """T8 — Mesh LLM：不允许的错误类型立即停止。"""

    def test_stops_on_not_allowed_error_class(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        service = MeshLLMBuildService(
            provider=_MeshFakeProvider(),
            executor=_MeshAlwaysFailNotAllowedExecutor(),
            config=MeshLLMRepairConfig(
                max_attempts=5,
                failure_class_filter=("runtime_error", "syntax_error"),
                repeated_failure_limit=3,
            ),
        )
        out = service.build(spec=_make_spec(), cad_result=_make_cad_result(run_dir),
                            output_dir=run_dir)

        assert out.success is False
        audit = json.loads(out.audit_path.read_text(encoding="utf-8"))
        assert audit["stop_reason"] == "failure_class_not_allowed"
        assert len(audit["attempts"]) == 1


# ---------------------------------------------------------------------------
# T9: Pipeline 自动回落：模板未命中时切换 LLM 模式
# ---------------------------------------------------------------------------

class TestPipelineAutoLLMFallback:
    """T9 — V3 "未命中再由LLM生成"：TemplateRegistry 返回 None 时自动切换 LLM。"""

    def test_auto_switches_cad_to_llm_when_no_template(self, tmp_path: Path) -> None:
        """
        当 TemplateRegistry.match() 返回 None 时，pipeline 应自动将 effective_cad_mode 切换为 "llm"。
        通过 mock _cad_llm_service 和 _cad_service 验证走了 LLM 路径而非 template 路径。
        """
        from autocae.backend.orchestrator.pipeline import PipelineRunner
        from autocae.backend.templates.registry import TemplateRegistry

        # mock：template registry 永远返回 None
        mock_registry = MagicMock(spec=TemplateRegistry)
        mock_registry.match.return_value = None

        runner = PipelineRunner(
            runs_dir=tmp_path / "runs",
            template_registry=mock_registry,
            dry_run=True,
            cad_mode="template",  # 显式设置为 template，但模板未命中应自动切换
            mesh_mode="template",
        )

        from autocae.backend.templates.cad.base import CADResult
        from autocae.schemas.mesh import GeometryMeta, GeometrySource, MeshGroups, MeshQualityReport
        from autocae.backend.services.cad_llm_service import CadLLMBuildOutcome
        from autocae.backend.services.mesh_llm_service import MeshLLMBuildOutcome

        spec = _make_spec()
        case_dir = tmp_path / "runs" / spec.metadata.case_id
        case_dir.mkdir(parents=True, exist_ok=True)

        geo_meta = GeometryMeta(
            step_file=str(case_dir / "model.step"), source=GeometrySource.CADQUERY,
            bounding_box={"xmin": -100.0, "xmax": 100.0, "ymin": -12.5,
                          "ymax": 12.5, "zmin": -1.0, "zmax": 1.0},
        )
        fake_cad_result = CADResult(step_file=case_dir / "model.step", geometry_meta=geo_meta)
        fake_cad_outcome = CadLLMBuildOutcome(
            success=True, cad_result=fake_cad_result,
            audit_path=case_dir / "cad_llm" / "audit.json",
            issue_report_path=None, message="mock cad llm success",
        )

        mesh_file = case_dir / "mesh.inp"
        fake_mesh_groups = MeshGroups(geometry_id="geo_test", mesh_file=str(mesh_file),
                                      groups=[], node_count=10, element_count=5)
        fake_mesh_quality = MeshQualityReport(
            geometry_id="geo_test", mesh_file=str(mesh_file),
            element_count=5, node_count=10, min_quality=0.8, avg_quality=0.9,
            max_aspect_ratio=1.5, checks=[], warnings=[], failed_checks=[], overall_pass=True,
        )
        fake_mesh_outcome = MeshLLMBuildOutcome(
            success=True, mesh_groups=fake_mesh_groups, mesh_quality=fake_mesh_quality,
            audit_path=case_dir / "mesh_llm" / "audit.json",
            issue_report_path=None, message="mock mesh llm success",
        )

        runner._cad_llm_service.build = MagicMock(return_value=fake_cad_outcome)
        runner._cad_service.build = MagicMock(side_effect=AssertionError(
            "template CAD service should NOT be called when template is None"
        ))
        runner._mesh_llm_service.build = MagicMock(return_value=fake_mesh_outcome)
        runner._mesh_service.build = MagicMock(side_effect=AssertionError(
            "template Mesh service should NOT be called when template is None"
        ))

        mock_summary = MagicMock()
        mock_summary.max_displacement = None
        mock_summary.max_mises_stress = None
        mock_summary.buckling_load_factor = None

        # patch _try_reuse_* 返回 None，强制走 build 路径（而非复用已有产物）
        with patch.object(runner, "_try_reuse_confirmed_cad", return_value=None):
            with patch.object(runner, "_try_reuse_confirmed_mesh", return_value=None):
                with patch("autocae.backend.orchestrator.pipeline.ensure_cad_gate_passed"):
                    with patch("autocae.backend.orchestrator.pipeline.ensure_mesh_gate_passed"):
                        with patch.object(runner._instantiator, "instantiate") as mock_inst:
                            mock_inst.return_value = MagicMock()
                            mock_inst.return_value.to_json.return_value = "{}"
                            with patch.object(runner._solver_adapter, "write_input", return_value={}):
                                with patch.object(runner._solver_adapter, "build_solver_job") as mj:
                                    mj.return_value = MagicMock()
                                    mj.return_value.job_id = "test_job"
                                    with patch.object(runner._postproc, "run") as mock_pp:
                                        mock_pp.return_value = (mock_summary, MagicMock(), MagicMock())
                                        runner.run(spec=spec)

        # LLM 服务应被调用（而非 template 服务）
        runner._cad_llm_service.build.assert_called_once()
        runner._mesh_llm_service.build.assert_called_once()

    def test_template_mode_used_when_template_found(self, tmp_path: Path) -> None:
        """模板命中时仍应使用 template 路径，不应切换到 LLM。"""
        from autocae.backend.orchestrator.pipeline import PipelineRunner
        from autocae.backend.templates.registry import TemplateRegistry, CaseTemplate
        from autocae.schemas.mesh import GeometryMeta, GeometrySource, MeshGroups, MeshQualityReport
        from autocae.backend.templates.cad.base import CADResult

        fake_template = MagicMock(spec=CaseTemplate)
        mock_registry = MagicMock(spec=TemplateRegistry)
        mock_registry.match.return_value = fake_template

        runner = PipelineRunner(
            runs_dir=tmp_path / "runs",
            template_registry=mock_registry,
            dry_run=True,
            cad_mode="template",
            mesh_mode="template",
        )
        runner._cad_llm_service.build = MagicMock(side_effect=AssertionError(
            "LLM CAD should NOT be called when template is found"
        ))

        spec = _make_spec()
        case_dir = tmp_path / "runs" / spec.metadata.case_id
        case_dir.mkdir(parents=True, exist_ok=True)

        geo_meta = GeometryMeta(
            step_file=str(case_dir / "model.step"), source=GeometrySource.CADQUERY,
            bounding_box={"xmin": 0, "xmax": 1, "ymin": 0, "ymax": 1, "zmin": 0, "zmax": 1},
        )
        fake_cad_result = CADResult(step_file=case_dir / "model.step", geometry_meta=geo_meta)
        mesh_file = case_dir / "mesh.inp"
        fake_mesh = (
            MeshGroups(geometry_id="g", mesh_file=str(mesh_file), groups=[], node_count=5, element_count=2),
            MeshQualityReport(geometry_id="g", mesh_file=str(mesh_file), element_count=2, node_count=5,
                              min_quality=0.8, avg_quality=0.9, max_aspect_ratio=1.5,
                              checks=[], warnings=[], failed_checks=[], overall_pass=True),
        )
        mock_summary = MagicMock()
        mock_summary.max_displacement = None
        mock_summary.max_mises_stress = None
        mock_summary.buckling_load_factor = None

        with patch.object(runner, "_try_reuse_confirmed_cad", return_value=None):
            with patch.object(runner, "_try_reuse_confirmed_mesh", return_value=None):
                with patch.object(runner._cad_service, "build", return_value=fake_cad_result) as mock_cad:
                    with patch.object(runner._mesh_service, "build", return_value=fake_mesh) as mock_mesh:
                        with patch("autocae.backend.orchestrator.pipeline.ensure_cad_gate_passed"):
                            with patch("autocae.backend.orchestrator.pipeline.ensure_mesh_gate_passed"):
                                with patch.object(runner._instantiator, "instantiate") as mock_inst:
                                    mock_inst.return_value = MagicMock()
                                    mock_inst.return_value.to_json.return_value = "{}"
                                    with patch.object(runner._solver_adapter, "write_input", return_value={}):
                                        with patch.object(runner._solver_adapter, "build_solver_job") as mj:
                                            mj.return_value = MagicMock()
                                            mj.return_value.job_id = "j1"
                                            with patch.object(runner._postproc, "run") as mp:
                                                mp.return_value = (mock_summary, MagicMock(), MagicMock())
                                                runner.run(spec=spec)

        mock_cad.assert_called_once()
        mock_mesh.assert_called_once()


# ---------------------------------------------------------------------------
# T10: RuleBasedCadScriptProvider — 无 API Key 时离线生成脚本
# ---------------------------------------------------------------------------

class TestRuleBasedCadScriptProviderOffline:
    """T10 — 无 API Key 时 CadLLMBuildService 使用 RuleBasedCadScriptProvider 生成脚本。"""

    def test_auto_provider_selects_rule_based_without_api_key(self) -> None:
        import os
        from autocae.backend.services.cad_llm_service import (
            CadLLMBuildService,
            RuleBasedCadScriptProvider,
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTOCAE_LLM_API_KEY", None)
            service = CadLLMBuildService()
        assert isinstance(service.provider, RuleBasedCadScriptProvider), (
            "无 API Key 时应自动选择 RuleBasedCadScriptProvider"
        )

    def test_rule_based_generates_valid_python_script(self) -> None:
        from autocae.backend.services.cad_llm_service import RuleBasedCadScriptProvider
        provider = RuleBasedCadScriptProvider()
        script = provider.generate_script(
            spec=_make_spec(),
            attempt=1,
            previous_script=None,
            error_context=None,
            output_dir=Path("/tmp/test"),
        )
        assert "cadquery" in script.script_text
        assert "--output-dir" in script.script_text
        assert "model.step" in script.script_text
        assert "geometry_meta.json" in script.script_text
        assert script.provider_meta["provider"] == "rule_based"

    def test_rule_based_generates_open_hole_variant(self) -> None:
        from autocae.backend.services.cad_llm_service import RuleBasedCadScriptProvider
        provider = RuleBasedCadScriptProvider()
        spec = _make_spec(geometry_type=GeometryType.OPEN_HOLE_PLATE)
        script = provider.generate_script(
            spec=spec, attempt=1, previous_script=None, error_context=None,
            output_dir=Path("/tmp/test"),
        )
        assert "hole" in script.script_text.lower(), "开孔平板脚本应包含 hole 操作"
