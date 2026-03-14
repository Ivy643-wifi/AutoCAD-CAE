"""Pipeline orchestrator — 协调完整的 8 阶段 AutoCAE 流水线。

Stage 0: Validate       → CaseSpecValidator
Stage 1: TemplateMatch  → TemplateRegistry
Stage 2: CAD            → CADService (CadQuery or external STEP)
Stage 3: Mesh           → MeshService (Gmsh)
Stage 4: AnalysisModel  → TemplateInstantiator
Stage 5: SolverInput    → CalculiXAdapter
Stage 6: SolverRun      → SolverRunner (or dry_run)
Stage 7: Postprocess    → PostprocessEngine

G-11（文件接口驱动）：每阶段通过 runs/<case_id>/ 目录内的文件交换数据。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from autocae.backend.input.loader import CaseSpecLoader
from autocae.backend.input.validator import CaseSpecValidator
from autocae.backend.services.cad_service import CADService
from autocae.backend.services.mesh_service import MeshService
from autocae.backend.services.solver_service import CalculiXAdapter, SolverRunner
from autocae.backend.services.postprocess_service import PostprocessEngine
from autocae.backend.templates.registry import TemplateRegistry
from autocae.backend.templates.instantiator import TemplateInstantiator
from autocae.schemas.analysis_model import AnalysisModel
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import MeshGroups, MeshQualityReport
from autocae.schemas.postprocess import Diagnostics, FieldManifest, ResultSummary
from autocae.schemas.solver import RunStatus, RunStatusEnum, SolverJob


@dataclass
class PipelineResult:
    """一次流水线运行的完整输出容器。"""
    case_id: str
    run_dir: Path
    success: bool = False
    error_message: str = ""
    analysis_model: AnalysisModel | None = None
    mesh_groups: MeshGroups | None = None
    mesh_quality: MeshQualityReport | None = None
    run_status: RunStatus | None = None
    result_summary: ResultSummary | None = None
    field_manifest: FieldManifest | None = None
    diagnostics: Diagnostics | None = None
    wall_time_s: float = 0.0


class PipelineRunner:
    """主控类：协调 Phase 1 AutoCAE 完整流水线。

    Usage::

        runner = PipelineRunner(runs_dir=Path("runs"))
        result = runner.run_from_yaml("examples/flat_plate_tension.yaml")

    dry_run=True → 跳过 Stage 6（CCX 求解），用于调试前置阶段。
    """

    def __init__(
        self,
        runs_dir: Path = Path("runs"),
        template_registry: TemplateRegistry | None = None,
        dry_run: bool = False,
        ccx_executable: str | None = None,
    ) -> None:
        self.runs_dir = Path(runs_dir)
        self.template_registry = template_registry or TemplateRegistry()
        self.dry_run = dry_run

        # 初始化所有阶段服务实例（单例，跨 run() 调用复用）
        self._loader        = CaseSpecLoader()
        self._validator     = CaseSpecValidator()
        self._cad_service   = CADService()
        self._mesh_service  = MeshService()
        self._instantiator  = TemplateInstantiator()
        self._solver_adapter = CalculiXAdapter()
        self._solver_runner  = SolverRunner(ccx_executable=ccx_executable)
        self._postproc       = PostprocessEngine()

    def run_from_yaml(self, yaml_path: str | Path) -> PipelineResult:
        """从 YAML 文件加载 CaseSpec 并运行完整流水线。"""
        return self.run(spec=self._loader.from_yaml(yaml_path))

    def run_from_json(self, json_path: str | Path) -> PipelineResult:
        """从 JSON 文件加载 CaseSpec 并运行完整流水线。"""
        return self.run(spec=self._loader.from_json(json_path))

    def run_from_yaml_with_step(
        self, yaml_path: str | Path, step_path: str | Path
    ) -> PipelineResult:
        """从 YAML 加载 CaseSpec，并使用外部 STEP 文件（G-02 备轨）。"""
        return self.run(spec=self._loader.from_yaml(yaml_path), step_file=Path(step_path))

    def run_from_json_with_step(
        self, json_path: str | Path, step_path: str | Path
    ) -> PipelineResult:
        """从 JSON 加载 CaseSpec，并使用外部 STEP 文件（G-02 备轨）。"""
        return self.run(spec=self._loader.from_json(json_path), step_file=Path(step_path))

    def run(self, spec: CaseSpec, step_file: Path | None = None) -> PipelineResult:
        """执行完整 8 阶段流水线（Stages 0-7）。

        任意阶段抛出异常时，捕获并记录错误，结果标记为失败（success=False）。
        """
        from datetime import datetime, timezone

        t_start = time.perf_counter()
        run_dir = self.runs_dir / spec.metadata.case_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result = PipelineResult(case_id=spec.metadata.case_id, run_dir=run_dir)

        try:
            # Stage 0：输入校验（Layer A 业务规则）
            logger.info(f"[Pipeline] Stage 0 — Validation (case={spec.metadata.case_id})")
            val_result = self._validator.validate(spec)
            if not val_result.passed:
                raise ValueError(
                    "CaseSpec validation failed:\n" + "\n".join(val_result.errors)
                )
            self._loader.save(spec, run_dir)

            # Stage 1：模板匹配（G-04）
            logger.info("[Pipeline] Stage 1 — Template Match")
            template = self.template_registry.match(spec)

            # Stage 2：CAD 几何生成（主轨 CadQuery / 备轨外部 STEP）
            logger.info(
                f"[Pipeline] Stage 2 — {'External STEP' if step_file else 'CAD Service'}"
            )
            cad_result = (
                self._cad_service.build_from_step(step_file, run_dir)
                if step_file
                else self._cad_service.build(spec, run_dir)
            )

            # Stage 3：网格生成（Gmsh）
            logger.info("[Pipeline] Stage 3 — Mesh Service")
            mesh_groups, mesh_quality = self._mesh_service.build(spec, cad_result, run_dir)
            result.mesh_groups = mesh_groups
            result.mesh_quality = mesh_quality
            if not mesh_quality.overall_pass:
                logger.warning("[Pipeline] Mesh quality below threshold — continuing anyway")

            # Stage 4：分析模型实例化
            logger.info("[Pipeline] Stage 4 — TemplateInstantiator")
            analysis_model = self._instantiator.instantiate(
                spec=spec,
                template=template,
                geometry_file=str(cad_result.step_file),
                geometry_meta_file=str(run_dir / "geometry_meta.json"),
            )
            result.analysis_model = analysis_model
            (run_dir / "analysis_model.json").write_text(
                analysis_model.to_json(), encoding="utf-8"
            )

            # Stage 5：求解器输入生成
            logger.info("[Pipeline] Stage 5 — CalculiXAdapter")
            input_files = self._solver_adapter.write_input(analysis_model, mesh_groups, run_dir)
            solver_job = self._solver_adapter.build_solver_job(analysis_model, input_files, run_dir)

            # Stage 6：求解器执行
            if self.dry_run:
                logger.info("[Pipeline] Stage 6 — Solver (DRY RUN — skipped)")
                now_utc = datetime.now(timezone.utc)
                run_status = RunStatus(
                    job_id=solver_job.job_id,
                    status=RunStatusEnum.COMPLETED,
                    start_time=now_utc,
                    end_time=now_utc,
                    wall_time_s=0.0,
                    result_files=[],
                )
            else:
                logger.info("[Pipeline] Stage 6 — Solver Execution")
                run_status = self._solver_runner.run(solver_job)
            result.run_status = run_status

            # Stage 7：后处理
            logger.info("[Pipeline] Stage 7 — PostprocessEngine")
            summary, manifest, diagnostics = self._postproc.run(
                run_status, analysis_model, run_dir
            )
            result.result_summary = summary
            result.field_manifest = manifest
            result.diagnostics = diagnostics
            result.success = True

        except Exception as exc:
            logger.exception(f"[Pipeline] FAILED: {exc}")
            result.success = False
            result.error_message = str(exc)

        result.wall_time_s = time.perf_counter() - t_start
        self._log_summary(result)
        return result

    def solve_from_run_dir(self, run_dir: str | Path) -> PipelineResult:
        """从已有 run 目录继续执行 Stage 6（求解）和 Stage 7（后处理）。

        适用场景：
          - 前次运行使用 --dry-run，现在想实际执行 CalculiX
          - 手动修改了 job.inp 后重新求解
          - 只需要重跑后处理（run_status.json 已存在）

        目录中必须已存在：
          - solver_job.json     → SolverJob
          - analysis_model.json → AnalysisModel
        """
        t_start = time.perf_counter()
        run_dir = Path(run_dir).resolve()
        case_id = run_dir.name
        result = PipelineResult(case_id=case_id, run_dir=run_dir)

        try:
            # 加载 solver_job.json 与 analysis_model.json
            job_path = run_dir / "solver_job.json"
            if not job_path.exists():
                raise FileNotFoundError(
                    f"solver_job.json not found in {run_dir}. "
                    "Run the full pipeline first (stages 0-5)."
                )
            solver_job = SolverJob.from_json(str(job_path))
            solver_job.working_dir = str(run_dir)

            am_path = run_dir / "analysis_model.json"
            if not am_path.exists():
                raise FileNotFoundError(f"analysis_model.json not found in {run_dir}.")
            analysis_model = AnalysisModel.model_validate_json(
                am_path.read_text(encoding="utf-8")
            )
            result.analysis_model = analysis_model

            # 加载 mesh_groups.json（后处理可能需要）
            mg_path = run_dir / "mesh_groups.json"
            if mg_path.exists():
                result.mesh_groups = MeshGroups.model_validate_json(
                    mg_path.read_text(encoding="utf-8")
                )

            # Stage 6：求解器执行（检查已完成状态，避免重复求解）
            status_path = run_dir / "run_status.json"
            if status_path.exists():
                existing_status = RunStatus.from_json(str(status_path))
                if existing_status.status == RunStatusEnum.COMPLETED:
                    logger.info(
                        "[Pipeline] Stage 6 — Solver already completed, skipping."
                    )
                    run_status = existing_status
                else:
                    logger.info(
                        f"[Pipeline] Stage 6 — Previous run status: "
                        f"{existing_status.status}. Re-running solver."
                    )
                    run_status = self._solver_runner.run(solver_job)
            else:
                logger.info("[Pipeline] Stage 6 — Solver Execution")
                run_status = self._solver_runner.run(solver_job)
            result.run_status = run_status

            # Stage 7：后处理
            logger.info("[Pipeline] Stage 7 — PostprocessEngine")
            summary, manifest, diagnostics = self._postproc.run(
                run_status, analysis_model, run_dir
            )
            result.result_summary = summary
            result.field_manifest = manifest
            result.diagnostics = diagnostics
            result.success = True

        except Exception as exc:
            logger.exception(f"[Pipeline] FAILED: {exc}")
            result.success = False
            result.error_message = str(exc)

        result.wall_time_s = time.perf_counter() - t_start
        self._log_summary(result)
        return result

    # ------------------------------------------------------------------

    def _log_summary(self, result: PipelineResult) -> None:
        """将流水线运行汇总信息输出到日志。"""
        status = "SUCCESS" if result.success else "FAILED"
        logger.info(
            f"[Pipeline] {status} | case={result.case_id} | "
            f"time={result.wall_time_s:.1f}s | dir={result.run_dir}"
        )
        if result.result_summary:
            s = result.result_summary
            if s.max_displacement is not None:
                logger.info(f"  max_displacement = {s.max_displacement:.4e} mm")
            if s.max_mises_stress is not None:
                logger.info(f"  max_mises_stress = {s.max_mises_stress:.4e} MPa")
            if s.buckling_load_factor is not None:
                logger.info(f"  buckling_load_factor = {s.buckling_load_factor:.4f}")
