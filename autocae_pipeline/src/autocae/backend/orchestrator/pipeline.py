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

import time
from dataclasses import dataclass
from pathlib import Path

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
    ensure_cad_gate_passed,
    ensure_mesh_gate_passed,
)
from autocae.backend.templates.registry import TemplateRegistry
from autocae.backend.templates.instantiator import TemplateInstantiator
from autocae.schemas.analysis_model import AnalysisModel
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import MeshGroups, MeshQualityReport
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
        from datetime import datetime, timezone

        t_start = time.perf_counter()
        run_dir = self.runs_dir / spec.metadata.case_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result = PipelineResult(case_id=spec.metadata.case_id, run_dir=run_dir)

        try:
            # Stage 0锛氳緭鍏ユ牎楠岋紙Layer A 涓氬姟瑙勫垯锛?
            logger.info(f"[Pipeline] Stage 0 鈥?Validation (case={spec.metadata.case_id})")
            val_result = self._validator.validate(spec)
            if not val_result.passed:
                raise ValueError(
                    "CaseSpec validation failed:\n" + "\n".join(val_result.errors)
                )
            self._loader.save(spec, run_dir)

            # Stage 1锛氭ā鏉垮尮閰嶏紙G-04锛?
            logger.info("[Pipeline] Stage 1 鈥?Template Match")
            template = self.template_registry.match(spec)

            # Stage 2锛欳AD 鍑犱綍鐢熸垚锛堜富杞?CadQuery / 澶囪建澶栭儴 STEP锛?
            logger.info(
                f"[Pipeline] Stage 2 鈥?{'External STEP' if step_file else self.cad_mode}"
            )
            if step_file:
                cad_result = self._cad_service.build_from_step(step_file, run_dir)
            elif self.cad_mode == "llm":
                llm_outcome = self._cad_llm_service.build(spec=spec, output_dir=run_dir)
                if not llm_outcome.success or llm_outcome.cad_result is None:
                    raise RuntimeError(
                        f"CAD LLM build failed: {llm_outcome.message}. "
                        f"audit={llm_outcome.audit_path}"
                    )
                cad_result = llm_outcome.cad_result
            else:
                cad_result = self._cad_service.build(spec, run_dir)

            # V3 CAD Gate锛氭湭閫氳繃瀹℃煡绂佹杩涘叆 mesh 闃舵
            logger.info("[Pipeline] CAD Gate check before Stage 3")
            ensure_cad_gate_passed(run_dir)

            # Stage 3锛氱綉鏍肩敓鎴愶紙Gmsh锛?
            logger.info(
                f"[Pipeline] Stage 3 鈥?{'mesh_llm' if self.mesh_mode == 'llm' else 'Mesh Service'}"
            )
            if self.mesh_mode == "llm":
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
                logger.warning("[Pipeline] Mesh quality below threshold 鈥?continuing anyway")

            # V3 Mesh Gate锛氭湭閫氳繃瀹℃煡绂佹杩涘叆鍚庣画闃舵
            logger.info("[Pipeline] Mesh Gate check before Stage 4+")
            ensure_mesh_gate_passed(run_dir)

            # Stage 4锛氬垎鏋愭ā鍨嬪疄渚嬪寲
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
            logger.info("[Pipeline] Stage 5 鈥?CalculiXAdapter")
            input_files = self._solver_adapter.write_input(analysis_model, mesh_groups, run_dir)
            solver_job = self._solver_adapter.build_solver_job(analysis_model, input_files, run_dir)

            # Stage 6锛氭眰瑙ｅ櫒鎵ц
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
        self._log_summary(result)
        return result

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

        try:
            # V3 Mesh Gate锛氭湭閫氳繃瀹℃煡绂佹杩涘叆姹傝В闃舵
            if enforce_mesh_gate:
                ensure_mesh_gate_passed(run_dir)

            # 鍔犺浇 solver_job.json 涓?analysis_model.json
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

            # 鍔犺浇 mesh_groups.json锛堝悗澶勭悊鍙兘闇€瑕侊級
            mg_path = run_dir / "mesh_groups.json"
            if mg_path.exists():
                result.mesh_groups = MeshGroups.model_validate_json(
                    mg_path.read_text(encoding="utf-8")
                )

            # Stage 6锛氭眰瑙ｅ櫒鎵ц锛堟鏌ュ凡瀹屾垚鐘舵€侊紝閬垮厤閲嶅姹傝В锛?            status_path = run_dir / "run_status.json"
            if status_path.exists():
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
        self._log_summary(result)
        return result

    # ------------------------------------------------------------------

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


