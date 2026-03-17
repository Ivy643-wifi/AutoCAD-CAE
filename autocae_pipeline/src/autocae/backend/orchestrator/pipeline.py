"""Pipeline orchestrator 鈥?鍗忚皟瀹屾暣鐨?8 闃舵 AutoCAE 娴佹按绾裤€?
Stage 0: Validate       鈫?CaseSpecValidator
Stage 1: TemplateMatch  鈫?TemplateRegistry
Stage 2: CAD            鈫?CADService (CadQuery or external STEP)
Stage 3: Mesh           鈫?MeshService (Gmsh)
Stage 4: AnalysisModel  鈫?TemplateInstantiator
Stage 5: SolverInput    鈫?CalculiXAdapter
Stage 6: SolverRun      鈫?SolverRunner (or dry_run)
Stage 7: Postprocess    鈫?PostprocessEngine

G-11锛堟枃浠舵帴鍙ｉ┍鍔級锛氭瘡闃舵閫氳繃 runs/<case_id>/ 鐩綍鍐呯殑鏂囦欢浜ゆ崲鏁版嵁銆?"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from autocae.backend.input.loader import CaseSpecLoader
from autocae.backend.input.validator import CaseSpecValidator
from autocae.backend.services.cad_service import CADService
from autocae.backend.services.cad_llm_service import (
    CadLLMBuildService,
    CadLLMRepairConfig,
)
from autocae.backend.services.mesh_llm_service import (
    MeshLLMBuildService,
    MeshLLMRepairConfig,
)
from autocae.backend.services.mesh_service import MeshService
from autocae.backend.services.solver_service import CalculiXAdapter, SolverRunner
from autocae.backend.services.postprocess_service import PostprocessEngine
from autocae.backend.review.gate_guard import (
    CadGateError,
    MeshGateError,
    ensure_cad_gate_passed,
    ensure_mesh_gate_passed,
)
from autocae.backend.orchestrator.artifact_locator import ArtifactLocator
from autocae.backend.templates.cad.base import CADResult
from autocae.backend.templates.registry import TemplateRegistry
from autocae.backend.templates.instantiator import TemplateInstantiator
from autocae.schemas.analysis_model import AnalysisModel
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, MeshGroups, MeshQualityReport
from autocae.schemas.postprocess import Diagnostics, FieldManifest, ResultSummary
from autocae.schemas.solver import RunStatus, RunStatusEnum, SolverJob


@dataclass
class PipelineResult:
    """Container for one pipeline run result."""
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
    """涓绘帶绫伙細鍗忚皟 Phase 1 AutoCAE 瀹屾暣娴佹按绾裤€?
    Usage::

        runner = PipelineRunner(runs_dir=Path("runs"))
        result = runner.run_from_yaml("examples/flat_plate_tension.yaml")

    dry_run=True 鈫?璺宠繃 Stage 6锛圕CX 姹傝В锛夛紝鐢ㄤ簬璋冭瘯鍓嶇疆闃舵銆?    """

    def __init__(
        self,
        runs_dir: Path = Path("runs"),
        template_registry: TemplateRegistry | None = None,
        dry_run: bool = False,
        ccx_executable: str | None = None,
        cad_mode: str = "template",
        cad_llm_max_attempts: int = 3,
        mesh_mode: str = "template",
        mesh_llm_max_attempts: int = 3,
    ) -> None:
        self.runs_dir = Path(runs_dir)
        self.template_registry = template_registry or TemplateRegistry()
        self.dry_run = dry_run
        if cad_mode not in {"template", "llm"}:
            raise ValueError(f"Unsupported cad_mode '{cad_mode}'. Expected 'template' or 'llm'.")
        self.cad_mode = cad_mode
        if mesh_mode not in {"template", "llm"}:
            raise ValueError(f"Unsupported mesh_mode '{mesh_mode}'. Expected 'template' or 'llm'.")
        self.mesh_mode = mesh_mode

        # 鍒濆鍖栨墍鏈夐樁娈垫湇鍔″疄渚嬶紙鍗曚緥锛岃法 run() 璋冪敤澶嶇敤锛?
        self._loader        = CaseSpecLoader()
        self._validator     = CaseSpecValidator()
        self._cad_service   = CADService()
        self._cad_llm_service = CadLLMBuildService(
            config=CadLLMRepairConfig(max_attempts=max(1, cad_llm_max_attempts))
        )
        self._mesh_llm_service = MeshLLMBuildService(
            config=MeshLLMRepairConfig(max_attempts=max(1, mesh_llm_max_attempts))
        )
        self._mesh_service  = MeshService()
        self._instantiator  = TemplateInstantiator()
        self._solver_adapter = CalculiXAdapter()
        self._solver_runner  = SolverRunner(ccx_executable=ccx_executable)
        self._postproc       = PostprocessEngine()

    def run_from_yaml(self, yaml_path: str | Path) -> PipelineResult:
        """Load CaseSpec from YAML and run the full pipeline."""
        return self.run(spec=self._loader.from_yaml(yaml_path))

    def run_from_json(self, json_path: str | Path) -> PipelineResult:
        """Load CaseSpec from JSON and run the full pipeline."""
        return self.run(spec=self._loader.from_json(json_path))

    def run_from_yaml_with_step(
        self, yaml_path: str | Path, step_path: str | Path
    ) -> PipelineResult:
        """Load YAML CaseSpec and use an external STEP file."""
        return self.run(spec=self._loader.from_yaml(yaml_path), step_file=Path(step_path))

    def run_from_json_with_step(
        self, json_path: str | Path, step_path: str | Path
    ) -> PipelineResult:
        """Load JSON CaseSpec and use an external STEP file."""
        return self.run(spec=self._loader.from_json(json_path), step_file=Path(step_path))

    def run(self, spec: CaseSpec, step_file: Path | None = None) -> PipelineResult:
        """鎵ц瀹屾暣 8 闃舵娴佹按绾匡紙Stages 0-7锛夈€?
        浠绘剰闃舵鎶涘嚭寮傚父鏃讹紝鎹曡幏骞惰褰曢敊璇紝缁撴灉鏍囪涓哄け璐ワ紙success=False锛夈€?        """
        t_start = time.perf_counter()
        run_dir = self.runs_dir / spec.metadata.case_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result = PipelineResult(case_id=spec.metadata.case_id, run_dir=run_dir)
        failed_stage = "unknown"
        locator = ArtifactLocator(run_dir)

        try:
            # Stage 0锛氳緭鍏ユ牎楠岋紙Layer A 涓氬姟瑙勫垯锛?
            failed_stage = "validation"
            logger.info(f"[Pipeline] Stage 0 鈥?Validation (case={spec.metadata.case_id})")
            val_result = self._validator.validate(spec)
            if not val_result.passed:
                raise ValueError(
                    "CaseSpec validation failed:\n" + "\n".join(val_result.errors)
                )
            self._loader.save(spec, run_dir)

            # Stage 1锛氭ā鏉垮尮閰嶏紙G-04锛?
            failed_stage = "template_match"
            logger.info("[Pipeline] Stage 1 鈥?Template Match")
            template = self.template_registry.match(spec)

            # V3 检索优先、LLM兜底：模板未命中时自动切换到 LLM 模式
            effective_cad_mode = self.cad_mode
            effective_mesh_mode = self.mesh_mode
            if template is None and not step_file:
                if self.cad_mode == "template":
                    logger.warning(
                        "[Pipeline] No template matched — auto-switching CAD to LLM mode (V3: 未命中再由LLM生成)"
                    )
                    effective_cad_mode = "llm"
                if self.mesh_mode == "template":
                    logger.warning(
                        "[Pipeline] No template matched — auto-switching Mesh to LLM mode (V3: 未命中再由LLM生成)"
                    )
                    effective_mesh_mode = "llm"

            # Stage 2锛欳AD 鍑犱綍鐢熸垚锛堜富杞?CadQuery / 澶囪建澶栭儴 STEP锛?
            failed_stage = "cad"
            logger.info(
                f"[Pipeline] Stage 2 鈥?{'External STEP' if step_file else effective_cad_mode}"
            )
            reused_cad = None if step_file is not None else self._try_reuse_confirmed_cad(
                run_dir=run_dir,
                locator=locator,
            )
            if reused_cad is not None:
                cad_result = reused_cad
                logger.info("[Pipeline] Reusing CAD artifacts from confirmed CAD gate.")
            else:
                if step_file:
                    cad_result = self._cad_service.build_from_step(step_file, run_dir)
                elif effective_cad_mode == "llm":
                    llm_outcome = self._cad_llm_service.build(spec=spec, output_dir=run_dir)
                    if not llm_outcome.success or llm_outcome.cad_result is None:
                        raise RuntimeError(
                            f"CAD LLM build failed: {llm_outcome.message}. "
                            f"audit={llm_outcome.audit_path}"
                        )
                    cad_result = llm_outcome.cad_result
                else:
                    cad_result = self._cad_service.build(spec, run_dir)

                # V3 CAD Gate：未通过审查禁止进入 mesh 阶段
                failed_stage = "cad_gate"
                logger.info("[Pipeline] CAD Gate check before Stage 3")
                ensure_cad_gate_passed(run_dir)

            # Stage 3锛氱綉鏍肩敓鎴愶紙Gmsh锛?
            failed_stage = "mesh"
            logger.info(
                f"[Pipeline] Stage 3 鈥?{'mesh_llm' if effective_mesh_mode == 'llm' else 'Mesh Service'}"
            )
            reused_mesh = self._try_reuse_confirmed_mesh(
                run_dir=run_dir,
                locator=locator,
            )
            if reused_mesh is not None:
                mesh_groups, mesh_quality = reused_mesh
                logger.info("[Pipeline] Reusing mesh artifacts from confirmed Mesh gate.")
            else:
                if effective_mesh_mode == "llm":
                    mesh_outcome = self._mesh_llm_service.build(
                        spec=spec,
                        cad_result=cad_result,
                        output_dir=run_dir,
                    )
                    if (
                        not mesh_outcome.success
                        or mesh_outcome.mesh_groups is None
                        or mesh_outcome.mesh_quality is None
                    ):
                        raise RuntimeError(
                            f"Mesh LLM build failed: {mesh_outcome.message}. "
                            f"audit={mesh_outcome.audit_path}"
                        )
                    mesh_groups = mesh_outcome.mesh_groups
                    mesh_quality = mesh_outcome.mesh_quality
                else:
                    mesh_groups, mesh_quality = self._mesh_service.build(spec, cad_result, run_dir)
            result.mesh_groups = mesh_groups
            result.mesh_quality = mesh_quality
            if not mesh_quality.overall_pass:
                logger.warning("[Pipeline] Mesh quality below threshold — continuing anyway")

            if reused_mesh is None:
                # V3 Mesh Gate：未通过审查禁止进入后续阶段
                failed_stage = "mesh_gate"
                logger.info("[Pipeline] Mesh Gate check before Stage 4+")
                ensure_mesh_gate_passed(run_dir)

            # Stage 4锛氬垎鏋愭ā鍨嬪疄渚嬪寲
            failed_stage = "analysis_model"
            logger.info("[Pipeline] Stage 4 鈥?TemplateInstantiator")
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

            # Stage 5锛氭眰瑙ｅ櫒杈撳叆鐢熸垚
            failed_stage = "solver_input"
            logger.info("[Pipeline] Stage 5 鈥?CalculiXAdapter")
            input_files = self._solver_adapter.write_input(analysis_model, mesh_groups, run_dir)
            solver_job = self._solver_adapter.build_solver_job(analysis_model, input_files, run_dir)

            # Stage 6锛氭眰瑙ｅ櫒鎵ц
            failed_stage = "solver_run"
            if self.dry_run:
                logger.info("[Pipeline] Stage 6 鈥?Solver (DRY RUN 鈥?skipped)")
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
                logger.info("[Pipeline] Stage 6 鈥?Solver Execution")
                run_status = self._solver_runner.run(solver_job)
            result.run_status = run_status

            # Stage 7锛氬悗澶勭悊
            failed_stage = "postprocess"
            logger.info("[Pipeline] Stage 7 鈥?PostprocessEngine")
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
        self._write_run_issue_report(
            result=result,
            error_stage="none" if result.success else failed_stage,
        )
        self._append_run_index(result=result, entry_type="run")
        self._log_summary(result)
        return result

    def _try_reuse_confirmed_cad(
        self,
        *,
        run_dir: Path,
        locator: ArtifactLocator,
    ) -> CADResult | None:
        try:
            ensure_cad_gate_passed(run_dir)
        except CadGateError:
            return None
        step_path = locator.resolve("step", required=False)
        meta_path = locator.resolve("geometry_meta", required=False)
        if step_path is None or meta_path is None:
            return None
        return CADResult(
            step_file=step_path,
            geometry_meta=GeometryMeta.from_json(str(meta_path)),
        )

    def _try_reuse_confirmed_mesh(
        self,
        *,
        run_dir: Path,
        locator: ArtifactLocator,
    ) -> tuple[MeshGroups, MeshQualityReport] | None:
        try:
            ensure_mesh_gate_passed(run_dir)
        except MeshGateError:
            return None
        groups_path = locator.resolve("mesh_groups", required=False)
        quality_path = locator.resolve("mesh_quality", required=False)
        if groups_path is None or quality_path is None:
            return None
        return (
            MeshGroups.from_json(str(groups_path)),
            MeshQualityReport.from_json(str(quality_path)),
        )

    def solve_from_run_dir(
        self,
        run_dir: str | Path,
        enforce_mesh_gate: bool = True,
    ) -> PipelineResult:
        """浠庡凡鏈?run 鐩綍缁х画鎵ц Stage 6锛堟眰瑙ｏ級鍜?Stage 7锛堝悗澶勭悊锛夈€?
        閫傜敤鍦烘櫙锛?          - 鍓嶆杩愯浣跨敤 --dry-run锛岀幇鍦ㄦ兂瀹為檯鎵ц CalculiX
          - 鎵嬪姩淇敼浜?job.inp 鍚庨噸鏂版眰瑙?          - 鍙渶瑕侀噸璺戝悗澶勭悊锛坮un_status.json 宸插瓨鍦級

        鐩綍涓繀椤诲凡瀛樺湪锛?          - solver_job.json     鈫?SolverJob
          - analysis_model.json 鈫?AnalysisModel
        """
        t_start = time.perf_counter()
        run_dir = Path(run_dir).resolve()
        case_id = run_dir.name
        result = PipelineResult(case_id=case_id, run_dir=run_dir)
        failed_stage = "unknown"
        locator = ArtifactLocator(run_dir)

        try:
            # V3 Mesh Gate锛氭湭閫氳繃瀹℃煡绂佹杩涘叆姹傝В闃舵
            if enforce_mesh_gate:
                failed_stage = "mesh_gate"
                ensure_mesh_gate_passed(run_dir)

            # 鍔犺浇 solver_job.json 涓?analysis_model.json
            failed_stage = "solver_input"
            job_path = locator.resolve("solver_job", required=False)
            if job_path is None:
                raise FileNotFoundError(
                    f"solver_job.json not found in {run_dir}. "
                    "Run the full pipeline first (stages 0-5)."
                )
            solver_job = SolverJob.from_json(str(job_path))
            solver_job.working_dir = str(run_dir)

            am_path = locator.resolve("analysis_model", required=False)
            if am_path is None:
                raise FileNotFoundError(f"analysis_model.json not found in {run_dir}.")
            analysis_model = AnalysisModel.model_validate_json(
                am_path.read_text(encoding="utf-8")
            )
            result.analysis_model = analysis_model

            # 鍔犺浇 mesh_groups.json锛堝悗澶勭悊鍙兘闇€瑕侊級
            mg_path = locator.resolve("mesh_groups", required=False)
            if mg_path is not None and mg_path.exists():
                result.mesh_groups = MeshGroups.model_validate_json(
                    mg_path.read_text(encoding="utf-8")
                )

            # Stage 6锛氭眰瑙ｅ櫒鎵ц锛堟鏌ュ凡瀹屾垚鐘舵€侊紝閬垮厤閲嶅姹傝В锛?            status_path = run_dir / "run_status.json"
            failed_stage = "solver_run"
            status_path = locator.resolve("run_status", required=False)
            if status_path is not None and status_path.exists():
                existing_status = RunStatus.from_json(str(status_path))
                if existing_status.status == RunStatusEnum.COMPLETED:
                    logger.info(
                        "[Pipeline] Stage 6 鈥?Solver already completed, skipping."
                    )
                    run_status = existing_status
                else:
                    logger.info(
                        f"[Pipeline] Stage 6 鈥?Previous run status: "
                        f"{existing_status.status}. Re-running solver."
                    )
                    run_status = self._solver_runner.run(solver_job)
            else:
                logger.info("[Pipeline] Stage 6 鈥?Solver Execution")
                run_status = self._solver_runner.run(solver_job)
            result.run_status = run_status

            # Stage 7锛氬悗澶勭悊
            failed_stage = "postprocess"
            logger.info("[Pipeline] Stage 7 鈥?PostprocessEngine")
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
        self._write_run_issue_report(
            result=result,
            error_stage="none" if result.success else failed_stage,
        )
        self._append_run_index(result=result, entry_type="solve")
        self._log_summary(result)
        return result

    # ------------------------------------------------------------------

    def _write_run_issue_report(
        self,
        *,
        result: PipelineResult,
        error_stage: str,
    ) -> Path:
        report = {
            "error_stage": error_stage,
            "error_message": "" if result.success else result.error_message,
            "root_cause_hint": self._root_cause_hint(error_stage, result.success),
            "remediation_hint": self._remediation_hint(error_stage, result.success),
            "success": result.success,
            "case_id": result.case_id,
            "run_dir": str(result.run_dir),
            "wall_time_s": result.wall_time_s,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        issue_path = result.run_dir / "issue_report.json"
        issue_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return issue_path

    def _append_run_index(self, *, result: PipelineResult, entry_type: str) -> Path:
        issue_path = result.run_dir / "issue_report.json"
        issue_payload: dict[str, Any] = {}
        if issue_path.exists():
            try:
                issue_payload = json.loads(issue_path.read_text(encoding="utf-8"))
            except Exception:
                issue_payload = {}

        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "entry_type": entry_type,
            "case_id": result.case_id,
            "run_dir": str(result.run_dir),
            "success": result.success,
            "wall_time_s": result.wall_time_s,
            "error_stage": issue_payload.get("error_stage", "unknown"),
            "error_message": issue_payload.get("error_message", result.error_message),
            "issue_report": str(issue_path),
        }
        index_path = result.run_dir.parent / "index.jsonl"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return index_path

    @staticmethod
    def _root_cause_hint(error_stage: str, success: bool) -> str:
        if success:
            return "Pipeline completed successfully."
        mapping = {
            "validation": "Input CaseSpec failed business-rule validation.",
            "template_match": "No suitable template matched current CaseSpec.",
            "cad": "CAD generation failed or produced invalid artifacts.",
            "cad_gate": "CAD gate was missing or not confirmed.",
            "mesh": "Mesh generation failed or produced invalid artifacts.",
            "mesh_gate": "Mesh gate was missing or not confirmed.",
            "analysis_model": "Analysis model instantiation failed.",
            "solver_input": "Solver input files were missing or invalid.",
            "solver_run": "Solver execution did not complete successfully.",
            "postprocess": "Postprocess failed to parse or summarize solver outputs.",
        }
        return mapping.get(error_stage, "Pipeline failed at an unknown stage.")

    @staticmethod
    def _remediation_hint(error_stage: str, success: bool) -> str:
        if success:
            return "No action required."
        mapping = {
            "validation": "Fix CaseSpec fields and run again.",
            "template_match": "Adjust geometry/analysis fields or add matching template.",
            "cad": "Inspect CAD logs/artifacts and regenerate geometry.",
            "cad_gate": "Run 'autocae review <run_dir> --stage cad' and confirm.",
            "mesh": "Inspect mesh logs/artifacts and regenerate mesh.",
            "mesh_gate": "Run 'autocae review <run_dir> --stage mesh' and confirm.",
            "analysis_model": "Check template inputs and regenerate analysis_model.json.",
            "solver_input": "Check solver_job.json and job.inp generation steps.",
            "solver_run": "Check solver executable/path and rerun solve stage.",
            "postprocess": "Check solver result files and rerun postprocess.",
        }
        return mapping.get(error_stage, "Inspect pipeline logs and issue_report.json for details.")

    def _log_summary(self, result: PipelineResult) -> None:
        """Write pipeline run summary to logs."""
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




