"""后处理引擎（PostprocessEngine）。

职责：
    将求解器的原始输出文件转换为结构化结果。

生成的输出文件：
    A. result_summary.json  — 标量关键结果（最大位移、最大 Mises 应力等）
    B. field_manifest.json  — 场量结果目录（记录哪些 VTK 文件可用）
    C. history_data.csv     — 载荷-位移历程及其他时间序列数据
    D. artifacts/           — PNG 图表（应力云图、位移云图、频率柱状图）
    E. diagnostics.json     — 收敛状态、警告信息、可信度评分（trust_level）

可视化技术选型：
    - 有限元场量云图：PyVista（基于 VTK，输出 .vtu 文件）
    - 载荷-位移曲线：matplotlib（输出 .png 文件）

主要流程（run() 方法）：
    1. 检查求解器运行状态（RunStatus）
    2. 找到 .frd 文件 → FRDParser 解析
    3. 构建 ResultSummary（提取最大位移、最大应力等关键标量）
    4. 构建 FieldManifest（导出每个场量的 .vtu 文件）
    5. 生成 PNG 图表
    6. 导出位移历程 CSV
    7. 保存所有 JSON 文件到运行目录
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

# 导入 FRD 解析器和后处理数据结构
from autocae.postprocess.frd_parser import FRDParser, FRDResult
from autocae.schemas.analysis_model import AnalysisModel
from autocae.schemas.postprocess import (
    Diagnostics,
    DiagnosticCheck,
    FieldManifest,
    FieldResult,
    ResultSummary,
    ScalarResult,
)
from autocae.schemas.solver import RunStatus, RunStatusEnum


class PostprocessEngine:
    """后处理引擎：将求解器输出文件转换为结构化结果。

    使用方式：
        engine = PostprocessEngine()
        summary, manifest, diagnostics = engine.run(
            run_status, analysis_model, output_dir
        )

    输入：
        run_status:     SolverRunner 返回的运行状态
        analysis_model: 当前分析的 AnalysisModel
        output_dir:     运行目录（同时读取 .frd 文件、写入结果文件）
    输出：
        ResultSummary   — 关键标量结果
        FieldManifest   — 场量文件目录
        Diagnostics     — 诊断报告（收敛状态、信任度）
    """

    def run(
        self,
        run_status: RunStatus,
        analysis_model: AnalysisModel,
        output_dir: Path,
    ) -> tuple[ResultSummary, FieldManifest, Diagnostics]:
        """解析结果，生成输出文件，保存 JSON 文件。

        参数说明：
            run_status:     求解器运行状态（来自 SolverRunner）
            analysis_model: 本次任务的 AnalysisModel 对象
            output_dir:     运行工作目录

        返回：
            (ResultSummary, FieldManifest, Diagnostics) 三元组
        """
        # 创建 artifacts/ 子目录，存放图片和 .vtu 文件
        artifacts_dir = output_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # 初始化诊断对象（默认为空，后续逐步填充）
        diagnostics = Diagnostics(
            job_id=run_status.job_id,
            analysis_id=analysis_model.metadata.analysis_id,
        )

        # ----------------------------------------------------------------
        # 第一关：检查求解器是否成功完成
        # ----------------------------------------------------------------
        if run_status.status != RunStatusEnum.COMPLETED:
            # 求解器未成功 → 记录错误，trust_level=0.0，返回空结果
            diagnostics.errors.append(
                f"Solver did not complete successfully: {run_status.error_message}"
            )
            diagnostics.convergence_achieved = False
            diagnostics.trust_level = 0.0
            self._save_outputs(output_dir, None, None, diagnostics)
            return (
                ResultSummary(
                    job_id=run_status.job_id,
                    analysis_id=analysis_model.metadata.analysis_id,
                ),
                FieldManifest(
                    job_id=run_status.job_id,
                    analysis_id=analysis_model.metadata.analysis_id,
                ),
                diagnostics,
            )

        # ----------------------------------------------------------------
        # 第二关：查找并解析 .frd 文件
        # ----------------------------------------------------------------
        # 从 RunStatus.result_files 列表中筛选 .frd 文件
        frd_files = [f for f in run_status.result_files if f.endswith(".frd")]
        if not frd_files:
            diagnostics.errors.append("No .frd result file found.")
            diagnostics.trust_level = 0.0
            self._save_outputs(output_dir, None, None, diagnostics)
            return self._empty_result(run_status, analysis_model, diagnostics)

        # 取第一个 .frd 文件（正常情况下只有一个）
        frd_path = frd_files[0]
        parser = FRDParser()
        try:
            frd_result = parser.parse(frd_path)  # → FRDResult 对象
        except Exception as exc:
            diagnostics.errors.append(f"FRD parse error: {exc}")
            diagnostics.trust_level = 0.0
            self._save_outputs(output_dir, None, None, diagnostics)
            return self._empty_result(run_status, analysis_model, diagnostics)

        # ----------------------------------------------------------------
        # 构建 ResultSummary（提取关键标量结果）
        # ----------------------------------------------------------------
        summary = self._build_summary(run_status, analysis_model, frd_result)

        # ----------------------------------------------------------------
        # 构建 FieldManifest 并尝试导出 VTK 文件
        # ----------------------------------------------------------------
        manifest = self._build_field_manifest(
            run_status, analysis_model, frd_result, artifacts_dir
        )

        # ----------------------------------------------------------------
        # 生成 PNG 图表（matplotlib）
        # ----------------------------------------------------------------
        self._export_plots(frd_result, summary, artifacts_dir)

        # ----------------------------------------------------------------
        # 导出载荷-位移历程 CSV 文件
        # ----------------------------------------------------------------
        self._export_history_csv(frd_result, output_dir)

        # ----------------------------------------------------------------
        # 最终化诊断信息（求解成功，trust_level=1.0）
        # ----------------------------------------------------------------
        diagnostics.convergence_achieved = True
        diagnostics.trust_level = 1.0
        diagnostics.checks.append(
            DiagnosticCheck(
                layer="runtime",                   # Layer C：运行时检查
                check_name="solver_convergence",   # 检查名称
                passed=True,
                message="Solver completed successfully.",
            )
        )

        # 持久化所有 JSON 结果文件
        self._save_outputs(output_dir, summary, manifest, diagnostics)
        return summary, manifest, diagnostics

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    def _build_summary(
        self,
        run_status: RunStatus,
        model: AnalysisModel,
        frd: FRDResult,
    ) -> ResultSummary:
        """从 FRDResult 提取关键标量，构建 ResultSummary 对象。

        提取内容：
            - 最大合位移及其节点 ID（调用 frd.max_displacement_magnitude()）
            - 最大 von Mises 应力及其节点 ID（调用 frd.max_mises_stress()）
            - 屈曲载荷因子（若有 BUCKLING 场量）
            - 固有频率列表（若有 FREQUENCY 场量，取前 10 阶）
        """
        # 从 FRDResult 方法中获取最大位移和应力
        max_disp, max_disp_node = frd.max_displacement_magnitude()
        max_mises, max_mises_node = frd.max_mises_stress()

        # 从 AnalysisModel 的分析步中获取分析类型（用于报告标题）
        step_names = [s.step_type.value for s in model.analysis_steps]
        analysis_type = step_names[0] if step_names else "unknown"

        # 构建 ResultSummary 基础字段
        summary = ResultSummary(
            job_id=run_status.job_id,
            analysis_id=model.metadata.analysis_id,
            max_displacement=max_disp if max_disp > 0 else None,
            max_displacement_node=max_disp_node if max_disp_node > 0 else None,
            max_mises_stress=max_mises if max_mises > 0 else None,
            max_mises_element=max_mises_node if max_mises_node > 0 else None,
            analysis_type=analysis_type,
        )

        # 附加屈曲载荷因子（线性屈曲分析才会有 BUCKLING 场量）
        buckle_field = frd.get_field("BUCKLING")
        if buckle_field and buckle_field.data:
            lf = list(buckle_field.data.values())[0]
            if lf:
                summary.buckling_load_factor = lf[0]  # 取第一个特征值（最小屈曲载荷因子）

        # 附加固有频率（模态分析才会有 FREQUENCY 场量）
        freq_field = frd.get_field("FREQUENCY")
        if freq_field and freq_field.data:
            freqs = [v[0] for v in freq_field.data.values() if v]
            if freqs:
                summary.natural_frequencies = sorted(freqs)[:10]  # 最多保存前 10 阶

        logger.info(
            f"  Summary: max_disp={max_disp:.4e} mm | "
            f"max_mises={max_mises:.4e} MPa"
        )
        return summary

    def _build_field_manifest(
        self,
        run_status: RunStatus,
        model: AnalysisModel,
        frd: FRDResult,
        artifacts_dir: Path,
    ) -> FieldManifest:
        """为每个解析出的场量生成 FieldResult 记录，并尝试导出 .vtu 文件。

        对每个场量（如 DISP、STRESS、RF）：
            1. 构造目标 .vtu 文件路径：artifacts/<field_name>_step<N>.vtu
            2. 尝试调用 _export_vtu()（需要 PyVista）
            3. 无论导出成功与否，都将记录添加到 manifest.fields
        """
        manifest = FieldManifest(
            job_id=run_status.job_id,
            analysis_id=model.metadata.analysis_id,
        )

        for field_data in frd.fields:
            # 生成 VTU 文件路径：artifacts/<field_name>_step<N>.vtu
            vtk_path = artifacts_dir / f"{field_data.field_name.lower()}_step{field_data.step}.vtu"

            # 尝试导出 VTU（若 PyVista 未安装或出错，returned=False）
            exported = self._export_vtu(frd, field_data, vtk_path)

            # 记录场量文件信息（即使未导出也记录，用 .NOT_EXPORTED 后缀标记）
            manifest.fields.append(
                FieldResult(
                    field_name=field_data.field_name,
                    step=str(field_data.step),
                    frame=field_data.increment,
                    region="whole_model",     # 全模型场量
                    storage_path=str(vtk_path) if exported else str(vtk_path) + ".NOT_EXPORTED",
                    components=field_data.components,
                )
            )
        return manifest

    def _export_vtu(
        self, frd: FRDResult, field_data: Any, vtk_path: Path
    ) -> bool:
        """尝试使用 PyVista 将场量导出为 VTU 文件（点云格式）。

        导出流程：
            1. 从 frd.nodes 构建点坐标数组（N×3）
            2. 创建 PyVista PolyData 点云对象
            3. 将场量数组（N×C）附加到点云上
            4. 保存为 .vtu 文件

        注意：
            - 若 PyVista 未安装，此方法静默失败并返回 False
            - 若节点数据为空，也直接返回 False
            - 导出格式为点云（PolyData），而非完整网格（UnstructuredGrid）

        返回：
            True  → 导出成功
            False → 导出失败（PyVista 不可用、数据为空、其他异常）
        """
        try:
            import pyvista as pv

            # 无节点数据 → 跳过
            if not frd.nodes:
                return False

            # 按节点 ID 排序，构建坐标矩阵（保证顺序与场量数组一致）
            node_ids = sorted(frd.nodes.keys())
            points = np.array([[frd.nodes[n].x, frd.nodes[n].y, frd.nodes[n].z]
                                for n in node_ids])
            cloud = pv.PolyData(points)  # 创建点云

            # 将场量值填入点数组
            if field_data.data:
                comp_count = max(len(v) for v in field_data.data.values())
                arr = np.zeros((len(node_ids), comp_count))  # 初始化为 0
                # 建立节点 ID → 数组行索引的映射
                id_to_idx = {nid: idx for idx, nid in enumerate(node_ids)}
                for nid, vals in field_data.data.items():
                    if nid in id_to_idx:
                        arr[id_to_idx[nid], :len(vals)] = vals
                cloud[field_data.field_name] = arr  # 挂载场量数组到点云

            cloud.save(str(vtk_path))
            return True

        except Exception as exc:
            # PyVista 不可用或发生异常 → 静默忽略，仅在 DEBUG 日志中记录
            logger.debug(f"VTU export skipped for {field_data.field_name}: {exc}")
            return False

    def _export_plots(
        self, frd: FRDResult, summary: ResultSummary, artifacts_dir: Path
    ) -> None:
        """使用 matplotlib 生成 PNG 图表（Phase 1 仅生成标量汇总图）。

        生成的图表：
            result_summary.png     — 水平条形图，展示最大位移、应力、屈曲载荷因子
            natural_frequencies.png — 柱状图，展示各阶固有频率（仅模态分析）

        说明：
            - 使用 'Agg' 后端（非交互式，适合无显示器的服务器环境）
            - 若 matplotlib 未安装或发生异常，静默跳过
        """
        try:
            import matplotlib
            matplotlib.use("Agg")   # 非交互式后端，不弹出窗口
            import matplotlib.pyplot as plt

            # 收集非空标量指标
            scalars: dict[str, float] = {}
            if summary.max_displacement is not None:
                scalars["Max Displacement (mm)"] = summary.max_displacement
            if summary.max_mises_stress is not None:
                scalars["Max Mises Stress (MPa)"] = summary.max_mises_stress
            if summary.buckling_load_factor is not None:
                scalars["Buckling Load Factor"] = summary.buckling_load_factor

            # 生成关键结果水平条形图
            if scalars:
                fig, ax = plt.subplots(figsize=(8, 4))
                keys = list(scalars.keys())
                vals = [scalars[k] for k in keys]
                bars = ax.barh(keys, vals, color="#4C72B0")  # 蓝色条形
                ax.set_xlabel("Value")
                ax.set_title(f"Key Results — {summary.analysis_type}")
                # 在条形右侧标注数值（科学计数法）
                for bar, val in zip(bars, vals):
                    ax.text(
                        bar.get_width() * 1.01, bar.get_y() + bar.get_height() / 2,
                        f"{val:.3e}", va="center", fontsize=9
                    )
                plt.tight_layout()
                plot_path = artifacts_dir / "result_summary.png"
                fig.savefig(str(plot_path), dpi=150)
                plt.close(fig)
                logger.info(f"  Summary plot saved → {plot_path}")

            # 固有频率柱状图（仅模态分析有数据）
            if summary.natural_frequencies:
                fig, ax = plt.subplots(figsize=(8, 4))
                freqs = summary.natural_frequencies
                modes = [f"Mode {i+1}" for i in range(len(freqs))]
                ax.bar(modes, freqs, color="#DD8452")  # 橙色柱状
                ax.set_ylabel("Frequency (Hz)")
                ax.set_title("Natural Frequencies")
                plt.tight_layout()
                freq_path = artifacts_dir / "natural_frequencies.png"
                fig.savefig(str(freq_path), dpi=150)
                plt.close(fig)

        except Exception as exc:
            # matplotlib 不可用或出错 → 警告并继续（图表是非关键输出）
            logger.warning(f"Plot generation skipped: {exc}")

    def _export_history_csv(self, frd: FRDResult, output_dir: Path) -> None:
        """将节点位移数据导出为 history_data.csv。

        CSV 格式：
            node_id, D1, D2, D3, [其他分量], U_magnitude
            （其中 U_magnitude = sqrt(D1² + D2² + D3²) 为合位移）

        用途：
            用于绘制载荷-位移曲线，或进一步的数据分析。
            Phase 1 中只导出最终步（get_field("DISP") 返回最后一步）。
        """
        # 获取最后一步的位移场量
        u_field = frd.get_field("DISP")
        if u_field is None or not u_field.data:
            return  # 无位移数据 → 跳过

        csv_path = output_dir / "history_data.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            # 表头：node_id + 各分量名 + U_magnitude（合位移）
            writer.writerow(["node_id"] + u_field.components + ["U_magnitude"])
            # 按节点 ID 排序写入数据行
            for nid in sorted(u_field.data.keys()):
                vals = u_field.data[nid]
                # 合位移：取前三个分量（U1, U2, U3）计算 L2 范数
                mag = float(np.sqrt(sum(v**2 for v in vals[:3]))) if len(vals) >= 3 else 0.0
                writer.writerow([nid] + vals + [mag])
        logger.info(f"  history_data.csv saved → {csv_path}")

    def _save_outputs(
        self,
        output_dir: Path,
        summary: ResultSummary | None,
        manifest: FieldManifest | None,
        diagnostics: Diagnostics,
    ) -> None:
        """将 ResultSummary、FieldManifest、Diagnostics 序列化并写入 JSON 文件。

        输出文件：
            result_summary.json   — 关键标量结果（若 summary 不为 None）
            field_manifest.json   — 场量文件目录（若 manifest 不为 None）
            diagnostics.json      — 诊断报告（始终写入）
        """
        if summary:
            p = output_dir / "result_summary.json"
            p.write_text(summary.to_json(), encoding="utf-8")
            logger.info(f"result_summary.json saved → {p}")
        if manifest:
            p = output_dir / "field_manifest.json"
            p.write_text(manifest.to_json(), encoding="utf-8")
            logger.info(f"field_manifest.json saved → {p}")
        # diagnostics.json 始终写入（即使求解失败也需要记录错误信息）
        p = output_dir / "diagnostics.json"
        p.write_text(diagnostics.to_json(), encoding="utf-8")
        logger.info(f"diagnostics.json saved → {p}")

    def _empty_result(
        self, run_status: RunStatus, model: AnalysisModel, diagnostics: Diagnostics
    ) -> tuple[ResultSummary, FieldManifest, Diagnostics]:
        """创建并返回空的 (ResultSummary, FieldManifest, Diagnostics) 三元组。

        在求解失败或 .frd 文件无法解析时调用，确保返回类型一致。
        """
        return (
            ResultSummary(job_id=run_status.job_id, analysis_id=model.metadata.analysis_id),
            FieldManifest(job_id=run_status.job_id, analysis_id=model.metadata.analysis_id),
            diagnostics,
        )
