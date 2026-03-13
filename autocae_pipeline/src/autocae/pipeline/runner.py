"""流水线主控器（PipelineRunner）— 协调 AutoCAE 完整流水线的执行。

Phase 1 主流程（8 个阶段）：
    Stage 0: Validate       → CaseSpecValidator  ：校验输入的合法性（Layer A 诊断）
    Stage 1: TemplateMatch  → TemplateRegistry   ：在模板库中匹配最相似的模板
    Stage 2: CAD            → CADBuilder         ：CadQuery 生成参数化几何，导出 model.step
    Stage 3: Mesh           → MeshBuilder        ：Gmsh 划分网格，导出 mesh.inp + mesh_groups.json
    Stage 4: AnalysisModel  → TemplateInstantiator：将 CaseSpec + 模板实例化为求解器无关的 AnalysisModel
    Stage 5: SolverInput    → CalculiXAdapter    ：将 AnalysisModel + MeshGroups 转换为 CalculiX job.inp
    Stage 6: SolverRun      → SolverRunner       ：调用 ccx 执行求解（dry_run 模式下跳过）
    Stage 7: Postprocess    → PostprocessEngine  ：解析 .frd，生成 ResultSummary / FieldManifest / 图表

设计原则 G-11（文件接口驱动）：
    每个阶段通过文件进行数据交换，所有中间产物都持久化到 runs/<case_id>/ 目录：
        case_spec.json          ← 第 0 阶段保存的标准化输入
        geometry_meta.json      ← 第 2 阶段 CAD Builder 输出
        model.step              ← 第 2 阶段 CadQuery 导出的 STEP 几何
        mesh.inp                ← 第 3 阶段 Gmsh 导出的网格（CalculiX 格式）
        mesh_groups.json        ← 第 3 阶段 Physical Group 信息
        mesh_quality_report.json← 第 3 阶段网格质量报告
        analysis_model.json     ← 第 4 阶段实例化的 FE 模型描述
        solver_job.json         ← 第 5 阶段求解任务元信息
        job.inp                 ← 第 5 阶段 CalculiX 输入文件
        run_status.json         ← 第 6 阶段运行状态
        result_summary.json     ← 第 7 阶段关键标量结果
        field_manifest.json     ← 第 7 阶段场量文件目录
        history_data.csv        ← 第 7 阶段位移历程数据
        diagnostics.json        ← 第 7 阶段诊断报告
        artifacts/              ← 第 7 阶段图表文件夹（PNG、VTU 等）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

# 导入各阶段的模块
from autocae.cad.builder import CADBuilder
from autocae.cad.step_handler import ExternalStepHandler
from autocae.case_spec.builder import CaseSpecBuilder
from autocae.case_spec.validator import CaseSpecValidator
from autocae.mesh.builder import MeshBuilder
from autocae.postprocess.engine import PostprocessEngine
from autocae.schemas.analysis_model import AnalysisModel
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import MeshGroups, MeshQualityReport
from autocae.schemas.postprocess import Diagnostics, FieldManifest, ResultSummary
from autocae.schemas.solver import RunStatus
from autocae.solver.calculix import CalculiXAdapter
from autocae.solver.runner import SolverRunner
from autocae.template_library.instantiator import TemplateInstantiator
from autocae.template_library.registry import TemplateRegistry


@dataclass
class PipelineResult:
    """一次流水线运行的完整输出容器。

    字段说明：
        case_id:        CaseSpec 的唯一标识符
        run_dir:        本次运行的工作目录（runs/<case_id>/）
        success:        流水线是否全程成功（True → 全部阶段通过）
        error_message:  失败时的错误信息（成功时为空字符串）

        analysis_model: 第 4 阶段产生的求解器无关 FE 模型
        mesh_groups:    第 3 阶段产生的 Physical Group 映射
        mesh_quality:   第 3 阶段产生的网格质量报告
        run_status:     第 6 阶段求解器运行状态（含结果文件路径）

        result_summary: 第 7 阶段的标量关键结果（位移、应力等）
        field_manifest: 第 7 阶段的场量文件目录（VTU 路径列表）
        diagnostics:    第 7 阶段的诊断报告（收敛状态、信任度）

        wall_time_s:    整个流水线的总耗时（秒）
    """
    case_id: str
    run_dir: Path
    success: bool = False
    error_message: str = ""
    # 中间阶段输出
    analysis_model: AnalysisModel | None = None
    mesh_groups: MeshGroups | None = None
    mesh_quality: MeshQualityReport | None = None
    run_status: RunStatus | None = None
    # 最终输出
    result_summary: ResultSummary | None = None
    field_manifest: FieldManifest | None = None
    diagnostics: Diagnostics | None = None
    # 性能计时
    wall_time_s: float = 0.0


class PipelineRunner:
    """协调 Phase 1 AutoCAE 完整流水线的主控类。

    使用方式（命令行入口 cli.py 会调用此类）：
        runner = PipelineRunner(runs_dir=Path("runs"))
        result = runner.run_from_yaml("examples/flat_plate_tension.yaml")

    dry_run 模式：
        PipelineRunner(dry_run=True)  → 跳过第 6 阶段（CCX 求解），
        直接创建 status=COMPLETED 的假 RunStatus，用于调试前几个阶段。
    """

    def __init__(
        self,
        runs_dir: Path = Path("runs"),
        template_registry: TemplateRegistry | None = None,
        dry_run: bool = False,
    ) -> None:
        """
        参数说明：
            runs_dir:          运行输出的根目录（每个 case 在此目录下创建子目录）
            template_registry: 预填充的模板注册表（为 None 时使用默认 TemplateRegistry）
            dry_run:           若为 True，跳过实际求解器执行（Stage 6）
        """
        self.runs_dir = Path(runs_dir)
        self.template_registry = template_registry or TemplateRegistry()
        self.dry_run = dry_run

        # 实例化各阶段模块（单例复用，避免重复构造开销）
        self._case_builder   = CaseSpecBuilder()       # YAML/JSON → CaseSpec
        self._validator      = CaseSpecValidator()     # CaseSpec 业务规则校验
        self._cad_builder    = CADBuilder()            # CadQuery 参数化几何
        self._mesh_builder   = MeshBuilder()           # Gmsh 网格划分
        self._instantiator   = TemplateInstantiator()  # CaseSpec + 模板 → AnalysisModel
        self._solver_adapter = CalculiXAdapter()       # AnalysisModel → job.inp
        self._solver_runner  = SolverRunner()          # 启动 ccx 子进程
        self._postproc       = PostprocessEngine()     # 解析 .frd → 结果

    # ------------------------------------------------------------------
    # 外部入口点
    # ------------------------------------------------------------------

    def run_from_yaml(self, yaml_path: str | Path) -> PipelineResult:
        """从 YAML 文件加载 CaseSpec，并运行完整流水线。

        这是命令行 `autocae run <yaml_path>` 的底层入口。
        """
        spec = self._case_builder.from_yaml(yaml_path)
        return self.run(spec)

    def run_from_json(self, json_path: str | Path) -> PipelineResult:
        """从 JSON 文件加载 CaseSpec，并运行完整流水线。"""
        spec = self._case_builder.from_json(json_path)
        return self.run(spec)

    def run_from_yaml_with_step(
        self, yaml_path: str | Path, step_path: str | Path
    ) -> PipelineResult:
        """从 YAML 文件加载 CaseSpec，并使用外部 STEP 文件跳过 CAD 生成阶段。

        这是命令行 `autocae run <yaml_path> --step-file <step_path>` 的底层入口。
        Stage 2（CadQuery 参数化建模）被替换为直接使用提供的 STEP 文件。
        """
        spec = self._case_builder.from_yaml(yaml_path)
        return self.run(spec, step_file=Path(step_path))

    def run_from_json_with_step(
        self, json_path: str | Path, step_path: str | Path
    ) -> PipelineResult:
        """从 JSON 文件加载 CaseSpec，并使用外部 STEP 文件跳过 CAD 生成阶段。"""
        spec = self._case_builder.from_json(json_path)
        return self.run(spec, step_file=Path(step_path))

    def run(self, spec: CaseSpec, step_file: Path | None = None) -> PipelineResult:
        """对给定的 CaseSpec 执行完整的 8 阶段流水线。

        错误处理策略：
            - 任意阶段抛出异常 → 捕获到最外层，设置 result.success=False
            - 所有中间产物（包括失败时已生成的）都保留在 run_dir 中，便于调试

        参数：
            spec:      已验证的（或未验证的，验证在 Stage 0 中进行）CaseSpec
            step_file: （可选）外部 STEP 文件路径。若提供，Stage 2 跳过 CadQuery
                       参数化建模，直接使用此 STEP 文件（G-02 双轨制备轨）。

        返回：
            PipelineResult（含所有阶段的输出和计时信息）
        """
        t_start = time.perf_counter()

        # 创建运行目录：runs/<case_id>/
        run_dir = self.runs_dir / spec.metadata.case_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result = PipelineResult(case_id=spec.metadata.case_id, run_dir=run_dir)

        try:
            # ----------------------------------------------------------
            # Stage 0：输入验证（Layer A 诊断）
            # ----------------------------------------------------------
            logger.info(f"[Pipeline] Stage 0 — Validation (case={spec.metadata.case_id})")
            val_result = self._validator.validate(spec)
            if not val_result.passed:
                # 验证不通过 → 立即终止（"早失败"原则）
                raise ValueError(
                    "CaseSpec validation failed:\n" + "\n".join(val_result.errors)
                )

            # 将 CaseSpec 序列化保存，供后续阶段和调试使用
            self._case_builder.save(spec, run_dir)

            # ----------------------------------------------------------
            # Stage 1：模板匹配
            # ----------------------------------------------------------
            logger.info("[Pipeline] Stage 1 — Template Match")
            # 在 TemplateRegistry 中查找最匹配的模板（G-04：模板优先）
            template = self.template_registry.match(spec)

            # ----------------------------------------------------------
            # Stage 2：CAD 几何生成
            # ----------------------------------------------------------
            if step_file is not None:
                # 备轨（G-02）：用户提供外部 STEP 文件，跳过 CadQuery 参数化建模
                logger.info(f"[Pipeline] Stage 2 — External STEP ({step_file})")
                cad_result = ExternalStepHandler().build(
                    step_path=step_file, output_dir=run_dir
                )
            else:
                # 主轨（G-02）：CadQuery 参数化建模 → 导出 model.step + geometry_meta.json
                logger.info("[Pipeline] Stage 2 — CAD Builder")
                cad_result = self._cad_builder.build(spec, run_dir)

            # ----------------------------------------------------------
            # Stage 3：网格划分
            # ----------------------------------------------------------
            logger.info("[Pipeline] Stage 3 — Mesh Builder")
            # Gmsh 导入 STEP → 划分网格 → 导出 mesh.inp + mesh_groups.json
            mesh_groups, mesh_quality = self._mesh_builder.build(spec, cad_result, run_dir)
            result.mesh_groups  = mesh_groups
            result.mesh_quality = mesh_quality

            if not mesh_quality.overall_pass:
                # 网格质量不达标 → 警告但继续（Phase 1 不中止）
                logger.warning("[Pipeline] Mesh quality below threshold — continuing anyway")

            # ----------------------------------------------------------
            # Stage 4：分析模型实例化
            # ----------------------------------------------------------
            logger.info("[Pipeline] Stage 4 — Analysis Model Instantiation")
            # TemplateInstantiator 将 CaseSpec + 模板 → 求解器无关的 AnalysisModel
            # 参数传入 geometry_meta_file 路径（用于读取包围盒信息）
            analysis_model = self._instantiator.instantiate(
                spec=spec,
                template=template,
                geometry_file=str(cad_result.step_file),
                geometry_meta_file=str(run_dir / "geometry_meta.json"),
            )
            result.analysis_model = analysis_model

            # 持久化 analysis_model.json（设计原则 G-11：文件接口）
            am_path = run_dir / "analysis_model.json"
            am_path.write_text(analysis_model.to_json(), encoding="utf-8")
            logger.info(f"analysis_model.json saved → {am_path}")

            # ----------------------------------------------------------
            # Stage 5：求解器输入文件组装
            # ----------------------------------------------------------
            logger.info("[Pipeline] Stage 5 — Solver Adapter (CalculiX)")
            # CalculiXAdapter.write_input() → 生成 job.inp
            input_files = self._solver_adapter.write_input(
                analysis_model, mesh_groups, run_dir
            )
            # CalculiXAdapter.build_solver_job() → 创建 SolverJob 描述符
            solver_job = self._solver_adapter.build_solver_job(
                analysis_model, input_files, run_dir
            )

            # ----------------------------------------------------------
            # Stage 6：求解器执行
            # ----------------------------------------------------------
            if self.dry_run:
                # dry_run 模式：跳过实际求解，直接构造一个"假的"成功状态
                # 用途：调试前几个阶段时不需要安装 CalculiX
                logger.info("[Pipeline] Stage 6 — Solver (DRY RUN — skipped)")
                from autocae.schemas.solver import RunStatus, RunStatusEnum
                from datetime import datetime
                run_status = RunStatus(
                    job_id=solver_job.job_id,
                    status=RunStatusEnum.COMPLETED,  # 假装已完成
                    start_time=datetime.utcnow(),
                    end_time=datetime.utcnow(),
                    wall_time_s=0.0,
                    result_files=[],  # 无实际结果文件
                )
            else:
                # 正式模式：调用 SolverRunner.run()，启动 ccx 子进程
                logger.info("[Pipeline] Stage 6 — Solver Execution")
                run_status = self._solver_runner.run(solver_job)
            result.run_status = run_status

            # ----------------------------------------------------------
            # Stage 7：后处理
            # ----------------------------------------------------------
            logger.info("[Pipeline] Stage 7 — Postprocess Engine")
            # PostprocessEngine 解析 .frd → ResultSummary + FieldManifest + Diagnostics
            summary, manifest, diagnostics = self._postproc.run(
                run_status, analysis_model, run_dir
            )
            result.result_summary = summary
            result.field_manifest = manifest
            result.diagnostics    = diagnostics
            result.success = True  # 全部阶段成功完成

        except Exception as exc:
            # 任意阶段异常 → 捕获，记录，继续返回（不向上抛出）
            logger.exception(f"[Pipeline] FAILED: {exc}")
            result.success = False
            result.error_message = str(exc)

        # 记录总耗时并打印摘要
        result.wall_time_s = time.perf_counter() - t_start
        self._print_summary(result)
        return result

    # ------------------------------------------------------------------

    def _print_summary(self, result: PipelineResult) -> None:
        """在日志中打印流水线运行摘要（状态 + 耗时 + 关键结果）。"""
        status = "SUCCESS" if result.success else "FAILED"
        logger.info(f"[Pipeline] {status} | case={result.case_id} | "
                    f"time={result.wall_time_s:.1f}s | dir={result.run_dir}")

        # 若有结果，打印关键标量（最大位移、最大应力、屈曲载荷因子）
        if result.result_summary:
            s = result.result_summary
            if s.max_displacement is not None:
                logger.info(f"  max_displacement = {s.max_displacement:.4e} mm")
            if s.max_mises_stress is not None:
                logger.info(f"  max_mises_stress = {s.max_mises_stress:.4e} MPa")
            if s.buckling_load_factor is not None:
                logger.info(f"  buckling_load_factor = {s.buckling_load_factor:.4f}")
