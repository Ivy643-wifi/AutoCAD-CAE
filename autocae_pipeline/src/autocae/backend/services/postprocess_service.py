"""Postprocess service — 后处理服务（Stage 7）。

职责：
  - 解析 CalculiX .frd 结果文件（FRDParser）
  - 提取关键标量结果（ResultSummary）
  - 导出 VTK / PNG / CSV 输出文件（PostprocessEngine）
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

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


# ---------------------------------------------------------------------------
# FRD 解析数据结构
# ---------------------------------------------------------------------------

@dataclass
class NodeData:
    """单个节点的坐标数据（从 .frd 文件节点块解析）。"""
    node_id: int
    x: float
    y: float
    z: float


@dataclass
class FieldData:
    """单个场量数据块（一个分析步的一个场量，如位移场 DISP）。"""
    field_name: str
    step: int
    increment: int
    components: list[str]                      # 场量分量名列表（如 ['D1', 'D2', 'D3']）
    data: dict[int, list[float]] = field(default_factory=dict)  # 节点 ID → 分量值列表


@dataclass
class FRDResult:
    """FRD 文件的完整解析结果容器。"""
    nodes: dict[int, NodeData] = field(default_factory=dict)   # 节点 ID → 坐标
    fields: list[FieldData] = field(default_factory=list)       # 所有场量数据块

    def get_field(self, name: str, step: int = -1) -> FieldData | None:
        """按场量名查找数据块；step=-1 返回最后一步（最终状态）。"""
        matches = [f for f in self.fields if f.field_name.upper() == name.upper()]
        if not matches:
            return None
        if step == -1:
            return matches[-1]  # 返回最后一个增量步（通常是最终状态）
        return next((f for f in matches if f.step == step), None)

    def max_displacement_magnitude(self) -> tuple[float, int]:
        """计算所有节点位移的最大合位移 ‖U‖₂，返回（最大值, 节点 ID）。"""
        u_field = self.get_field("DISP")
        if u_field is None:
            return 0.0, -1
        max_val = 0.0; max_node = -1
        for nid, vals in u_field.data.items():
            if len(vals) >= 3:
                # 合位移 = sqrt(Ux² + Uy² + Uz²)
                mag = float(np.sqrt(vals[0]**2 + vals[1]**2 + vals[2]**2))
                if mag > max_val:
                    max_val = mag; max_node = nid
        return max_val, max_node

    def load_displacement_curve(self) -> list[tuple[float, float]]:
        """计算载荷-位移曲线各数据点（每个增量步一个点）。

        返回值：列表 [(total_load_N, max_disp_mm), ...]，按步号升序排列。

        - 位移：全模型最大合位移 ||U||₂ [mm]
        - 载荷：所有有反力记录节点的反力合力之和 [N]
          （固支节点的反力合力 ≈ 施加载荷的平衡力）

        对单增量步线性分析，自动在头部插入原点 (0.0, 0.0) 构成两点线性曲线。
        """
        disp_by_step = {
            f.step: f for f in self.fields if f.field_name.upper() == "DISP"
        }
        rf_by_step = {
            f.step: f for f in self.fields if f.field_name.upper() == "RF"
        }

        points: list[tuple[float, float]] = []
        for step_num in sorted(disp_by_step.keys()):
            d_field = disp_by_step[step_num]

            # 最大合位移
            max_disp = 0.0
            for vals in d_field.data.values():
                if len(vals) >= 3:
                    mag = float(np.sqrt(vals[0] ** 2 + vals[1] ** 2 + vals[2] ** 2))
                    if mag > max_disp:
                        max_disp = mag

            # 反力合力（所有有反力节点的 ||RF||₂ 之和）
            total_load = 0.0
            if step_num in rf_by_step:
                for vals in rf_by_step[step_num].data.values():
                    if len(vals) >= 3:
                        total_load += float(
                            np.sqrt(vals[0] ** 2 + vals[1] ** 2 + vals[2] ** 2)
                        )

            points.append((total_load, max_disp))

        # 单步线性分析：插入原点，得到两点直线
        if len(points) == 1:
            points.insert(0, (0.0, 0.0))

        return points

    def max_mises_stress(self) -> tuple[float, int]:
        """计算最大 von Mises 等效应力，返回（最大值 [MPa], 节点 ID）。

        策略：
            1. 优先使用 FRD 中直接输出的 MISES 分量（如 'V' 列）
            2. 若无，则从 6 个应力分量手动计算 Mises 公式
        """
        s_field = self.get_field("STRESS")
        if s_field is None:
            return 0.0, -1

        # 检查是否有直接输出的 Mises 分量（CalculiX 可输出 'V' = von Mises）
        mises_idx = None
        for i, comp in enumerate(s_field.components):
            if "MISES" in comp.upper() or comp.upper() == "V":
                mises_idx = i; break

        if mises_idx is None and len(s_field.components) >= 6:
            # 无直接 Mises 分量，从 6 分量应力张量（Sx, Sy, Sz, Sxy, Sxz, Syz）手动计算
            max_val = 0.0; max_node = -1
            for nid, vals in s_field.data.items():
                sx, sy, sz = vals[0], vals[1], vals[2]
                sxy, sxz, syz = vals[3], vals[4], vals[5]
                # von Mises 公式：σ_vm = sqrt(0.5*[(σx-σy)²+(σy-σz)²+(σz-σx)²+6(τxy²+τxz²+τyz²)])
                mises = float(np.sqrt(0.5 * (
                    (sx-sy)**2 + (sy-sz)**2 + (sz-sx)**2
                    + 6*(sxy**2 + sxz**2 + syz**2)
                )))
                if mises > max_val:
                    max_val = mises; max_node = nid
            return max_val, max_node

        if mises_idx is not None:
            # 有直接 Mises 分量，直接读取
            max_val = 0.0; max_node = -1
            for nid, vals in s_field.data.items():
                if mises_idx < len(vals):
                    v = abs(vals[mises_idx])
                    if v > max_val:
                        max_val = v; max_node = nid
            return max_val, max_node

        return 0.0, -1


class FRDParser:
    """CalculiX .frd 结果文件的文本解析器。

    .frd 是 CalculiX 专有的二进制/ASCII 混合格式，
    本解析器仅处理 ASCII 模式（ASCII FRD）。

    FRD 文件结构：
        2C 块    → 节点坐标定义（node block）
        100CL/-1 块 → 场量结果数据（result block，位移/应力/反力等）
        每个块以 " -3" 行结束
    """

    # 场量名别名映射：FRD 中的旧/变体名 → 统一内部名
    _FIELD_ALIASES: dict[str, str] = {
        "DISPLACEMENTS": "DISP",    # 位移场
        "DISP(CO)":      "DISP",
        "STRESSES":      "STRESS",  # 应力场
        "STRESS(CO)":    "STRESS",
        "FORC":          "RF",      # 反力场
        "FORC(CO)":      "RF",
        "REACTIONS":     "RF",
    }

    def parse(self, frd_path: str | Path) -> FRDResult:
        """读取并解析 .frd 文件，返回 FRDResult 对象。"""
        path = Path(frd_path)
        if not path.exists():
            raise FileNotFoundError(f"FRD file not found: {path}")

        logger.info(f"FRDParser: parsing {path}")
        result = FRDResult()

        # .frd 文件用 latin-1 编码（包含非 ASCII 字符）
        with open(path, encoding="latin-1") as fh:
            lines = fh.readlines()

        i = 0
        while i < len(lines):
            line = lines[i].rstrip("\n")
            if line.startswith("    2C"):
                # 节点坐标块（以 "    2C" 开头）
                i = self._parse_node_block(lines, i, result); continue
            if line.startswith("    3C"):
                # 单元连接块 — 跳过整块到 " -3" 终止符，避免误触发结果块解析
                i += 1
                while i < len(lines) and not lines[i].startswith(" -3"):
                    i += 1
                i += 1  # 跳过 " -3" 行本身
                continue
            if "100CL" in line:
                # 结果场量块头（100CL 为块头前缀，可能有前导空格）
                i = self._parse_result_block(lines, i, result); continue
            i += 1

        logger.info(
            f"  Parsed {len(result.nodes)} nodes, {len(result.fields)} field blocks"
        )
        return result

    def _parse_node_block(self, lines: list[str], start: int, result: FRDResult) -> int:
        """解析节点坐标块，将节点坐标存入 result.nodes。

        FRD 节点行格式（固定宽度）：
            " -1" (3) + node_id (10) + x (12-13) + y (12-13) + z (12-13)
        Windows CalculiX 使用 3 位指数（E+002），坐标值紧邻无分隔符。
        块结束标志：" -3"
        """
        float_re = re.compile(r"[+-]?\d+\.\d+[Ee][+-]?\d{1,3}")
        i = start + 1
        while i < len(lines):
            line = lines[i].rstrip("\n")
            if line.startswith(" -3"):
                return i + 1  # 块结束，返回下一行索引
            if line.startswith(" -1"):
                try:
                    nid = int(line[3:13])
                    floats = float_re.findall(line[13:])
                    if len(floats) >= 3:
                        x, y, z = float(floats[0]), float(floats[1]), float(floats[2])
                        result.nodes[nid] = NodeData(nid, x, y, z)
                except (ValueError, IndexError):
                    pass
            i += 1
        return i

    def _parse_result_block(self, lines: list[str], start: int, result: FRDResult) -> int:
        """解析场量结果块，将数据存入 result.fields。

        FRD 结果块格式：
            头行（含场量名和步号）
            " -4" 行 → 分量定义（随后的 " -5" 行每行一个分量名）
            " -1" 行 → 节点数据（node_id + 分量值列表）
        块结束标志：" -3"
        """
        float_re = re.compile(r"[+-]?\d+\.\d+[Ee][+-]?\d{1,3}")
        header_line = lines[start].rstrip("\n")
        field_name = self._extract_field_name(header_line)
        step_num = self._extract_step_number(header_line)
        # 将别名统一为内部标准名（如 DISPLACEMENTS → DISP）
        field_name = self._FIELD_ALIASES.get(field_name, field_name)

        components: list[str] = []
        data: dict[int, list[float]] = {}

        i = start + 1
        while i < len(lines):
            line = lines[i].rstrip("\n")
            if line.startswith(" -3"):
                # 块结束：将本场量存入结果（若有分量定义才存）
                if components:
                    result.fields.append(FieldData(
                        field_name=field_name,
                        step=step_num,
                        increment=0,
                        components=components,
                        data=data,
                    ))
                return i + 1
            if line.startswith(" -4"):
                # 分量定义块头：场量名在 -4 行的第二个 token（如 " -4  DISP  4  1"）
                parts_4 = line.split()
                if len(parts_4) >= 2:
                    raw_name = parts_4[1].upper()
                    field_name = self._FIELD_ALIASES.get(raw_name, raw_name)
                i += 1
                while i < len(lines) and lines[i].startswith(" -5"):
                    comp_line = lines[i].rstrip("\n")
                    parts = comp_line.split()
                    if len(parts) >= 2:
                        components.append(parts[1])
                    i += 1
                continue
            if line.startswith(" -1"):
                # 节点数据行（固定宽度格式）：" -1" (3) + node_id (10) + 各分量值（紧邻）
                try:
                    nid = int(line[3:13])
                    floats = float_re.findall(line[13:])
                    vals = [float(v) for v in floats]
                    if nid in data:
                        data[nid].extend(vals)  # 同一节点分多行时追加
                    else:
                        data[nid] = vals
                except (ValueError, IndexError):
                    pass
            i += 1
        return i

    @staticmethod
    def _extract_field_name(line: str) -> str:
        """从结果块头行中提取场量名（跳过纯数字 token）。"""
        parts = line.split()
        for p in parts[1:]:
            if p and not p.lstrip("-").replace(".", "").isdigit():
                return p.upper()
        return "UNKNOWN"

    @staticmethod
    def _extract_step_number(line: str) -> int:
        """从头行中提取分析步编号（如 'STEP=2' → 2）；未找到则返回 1。"""
        m = re.search(r"STEP\s*=?\s*(\d+)", line, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return 1


# ---------------------------------------------------------------------------
# 后处理引擎
# ---------------------------------------------------------------------------

class PostprocessEngine:
    """Stage 7 服务：解析求解器输出 → 结构化结果。"""

    def run(
        self,
        run_status: RunStatus,
        analysis_model: AnalysisModel,
        output_dir: Path,
    ) -> tuple[ResultSummary, FieldManifest, Diagnostics]:
        """执行完整后处理流程，返回 (ResultSummary, FieldManifest, Diagnostics)。

        流程：
            1. 检查求解器是否成功完成
            2. 定位 .frd 结果文件
            3. 解析 FRD → 提取标量结果和场量数据
            4. 导出 VTK / PNG / CSV 文件
            5. 构建并写入诊断报告
        """
        artifacts_dir = output_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        diagnostics = Diagnostics(
            job_id=run_status.job_id,
            analysis_id=analysis_model.metadata.analysis_id,
        )

        # 求解器未成功完成 → 返回空结果，可信度 0
        if run_status.status != RunStatusEnum.COMPLETED:
            diagnostics.errors.append(
                f"Solver did not complete successfully: {run_status.error_message}"
            )
            diagnostics.convergence_achieved = False
            diagnostics.trust_level = 0.0
            self._save_outputs(output_dir, None, None, diagnostics)
            return self._empty_result(run_status, analysis_model, diagnostics)

        # 查找 .frd 结果文件
        frd_files = [f for f in run_status.result_files if f.endswith(".frd")]
        if not frd_files:
            diagnostics.errors.append("No .frd result file found.")
            diagnostics.trust_level = 0.0
            self._save_outputs(output_dir, None, None, diagnostics)
            return self._empty_result(run_status, analysis_model, diagnostics)

        # 解析第一个 .frd 文件（多 .frd 的情况极少见）
        frd_path = frd_files[0]
        parser = FRDParser()
        try:
            frd_result = parser.parse(frd_path)
        except Exception as exc:
            diagnostics.errors.append(f"FRD parse error: {exc}")
            diagnostics.trust_level = 0.0
            self._save_outputs(output_dir, None, None, diagnostics)
            return self._empty_result(run_status, analysis_model, diagnostics)

        # 提取标量结果、构建场量目录、导出图像和 CSV
        summary = self._build_summary(run_status, analysis_model, frd_result)
        manifest = self._build_field_manifest(run_status, analysis_model, frd_result, artifacts_dir)
        self._export_plots(frd_result, summary, artifacts_dir)
        self._export_load_displacement(frd_result, summary, output_dir)
        self._export_history_csv(frd_result, output_dir)

        # 标记诊断通过，可信度设为 1.0
        diagnostics.convergence_achieved = True
        diagnostics.trust_level = 1.0
        diagnostics.checks.append(
            DiagnosticCheck(
                layer="runtime",
                check_name="solver_convergence",
                passed=True,
                message="Solver completed successfully.",
            )
        )

        self._save_outputs(output_dir, summary, manifest, diagnostics)
        return summary, manifest, diagnostics

    # ------------------------------------------------------------------
    # 私有辅助方法
    # ------------------------------------------------------------------

    def _build_summary(
        self, run_status: RunStatus, model: AnalysisModel, frd: FRDResult
    ) -> ResultSummary:
        """从 FRDResult 提取关键标量结果，构建 ResultSummary。"""
        max_disp, max_disp_node = frd.max_displacement_magnitude()
        max_mises, max_mises_node = frd.max_mises_stress()
        # 从分析步类型推断分析类型字符串（用于结果表格显示）
        step_names = [s.step_type.value for s in model.analysis_steps]
        analysis_type = step_names[0] if step_names else "unknown"

        summary = ResultSummary(
            job_id=run_status.job_id,
            analysis_id=model.metadata.analysis_id,
            max_displacement=max_disp if max_disp > 0 else None,
            max_displacement_node=max_disp_node if max_disp_node > 0 else None,
            max_mises_stress=max_mises if max_mises > 0 else None,
            max_mises_element=max_mises_node if max_mises_node > 0 else None,
            analysis_type=analysis_type,
        )

        # 提取屈曲载荷因子（若有屈曲结果）
        buckle_field = frd.get_field("BUCKLING")
        if buckle_field and buckle_field.data:
            lf = list(buckle_field.data.values())[0]
            if lf:
                summary.buckling_load_factor = lf[0]

        # 提取固有频率列表（取前 10 阶，升序排列）
        freq_field = frd.get_field("FREQUENCY")
        if freq_field and freq_field.data:
            freqs = [v[0] for v in freq_field.data.values() if v]
            if freqs:
                summary.natural_frequencies = sorted(freqs)[:10]

        # 计算载荷-位移曲线（每步一个数据点）
        ld_curve = frd.load_displacement_curve()
        if ld_curve:
            summary.load_displacement_curve = [list(pt) for pt in ld_curve]

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
        """为每个场量创建 VTU 文件并构建 FieldManifest（场量目录）。"""
        manifest = FieldManifest(
            job_id=run_status.job_id,
            analysis_id=model.metadata.analysis_id,
        )
        for field_data in frd.fields:
            # VTU 文件命名：<field_name_lower>_step<N>.vtu
            vtk_path = artifacts_dir / f"{field_data.field_name.lower()}_step{field_data.step}.vtu"
            exported = self._export_vtu(frd, field_data, vtk_path)
            manifest.fields.append(
                FieldResult(
                    field_name=field_data.field_name,
                    step=str(field_data.step),
                    frame=field_data.increment,
                    region="whole_model",
                    # 若导出失败，在路径末尾追加 .NOT_EXPORTED 标记
                    storage_path=str(vtk_path) if exported else str(vtk_path) + ".NOT_EXPORTED",
                    components=field_data.components,
                )
            )
        return manifest

    def _export_vtu(self, frd: FRDResult, field_data: Any, vtk_path: Path) -> bool:
        """将场量数据导出为 PyVista VTU 点云文件。

        若 PyVista 未安装或节点数据为空，则跳过导出并返回 False。
        """
        try:
            import pyvista as pv
            if not frd.nodes:
                return False
            # 构造节点坐标数组（按节点 ID 排序保证一致性）
            node_ids = sorted(frd.nodes.keys())
            points = np.array([[frd.nodes[n].x, frd.nodes[n].y, frd.nodes[n].z]
                                for n in node_ids])
            cloud = pv.PolyData(points)
            if field_data.data:
                # 将场量数据数组附加到点云
                comp_count = max(len(v) for v in field_data.data.values())
                arr = np.zeros((len(node_ids), comp_count))
                id_to_idx = {nid: idx for idx, nid in enumerate(node_ids)}
                for nid, vals in field_data.data.items():
                    if nid in id_to_idx:
                        arr[id_to_idx[nid], :len(vals)] = vals
                cloud[field_data.field_name] = arr
            cloud.save(str(vtk_path))
            return True
        except Exception as exc:
            logger.debug(f"VTU export skipped for {field_data.field_name}: {exc}")
            return False

    def _export_plots(
        self, frd: FRDResult, summary: ResultSummary, artifacts_dir: Path
    ) -> None:
        """用 matplotlib 生成关键结果条形图和固有频率柱状图（PNG 格式）。

        若 matplotlib 未安装或生成失败，记录警告并跳过。
        """
        try:
            import matplotlib
            matplotlib.use("Agg")  # 非交互式后端（无 GUI 服务器环境必须设置）
            import matplotlib.pyplot as plt

            # 收集可用的关键标量结果
            scalars: dict[str, float] = {}
            if summary.max_displacement is not None:
                scalars["Max Displacement (mm)"] = summary.max_displacement
            if summary.max_mises_stress is not None:
                scalars["Max Mises Stress (MPa)"] = summary.max_mises_stress
            if summary.buckling_load_factor is not None:
                scalars["Buckling Load Factor"] = summary.buckling_load_factor

            if scalars:
                # 关键结果水平条形图（result_summary.png）
                fig, ax = plt.subplots(figsize=(8, 4))
                keys = list(scalars.keys())
                vals = [scalars[k] for k in keys]
                bars = ax.barh(keys, vals, color="#4C72B0")
                ax.set_xlabel("Value")
                ax.set_title(f"Key Results — {summary.analysis_type}")
                # 在每条条形右侧标注科学计数法数值
                for bar, val in zip(bars, vals):
                    ax.text(
                        bar.get_width() * 1.01, bar.get_y() + bar.get_height() / 2,
                        f"{val:.3e}", va="center", fontsize=9
                    )
                plt.tight_layout()
                fig.savefig(str(artifacts_dir / "result_summary.png"), dpi=150)
                plt.close(fig)

            if summary.natural_frequencies:
                # 固有频率柱状图（natural_frequencies.png）
                fig, ax = plt.subplots(figsize=(8, 4))
                freqs = summary.natural_frequencies
                modes = [f"Mode {i+1}" for i in range(len(freqs))]
                ax.bar(modes, freqs, color="#DD8452")
                ax.set_ylabel("Frequency (Hz)")
                ax.set_title("Natural Frequencies")
                plt.tight_layout()
                fig.savefig(str(artifacts_dir / "natural_frequencies.png"), dpi=150)
                plt.close(fig)

        except Exception as exc:
            logger.warning(f"Plot generation skipped: {exc}")

    def _export_load_displacement(
        self, frd: FRDResult, summary: ResultSummary, output_dir: Path
    ) -> None:
        """将载荷-位移曲线保存为 CSV 和 PNG 文件。

        CSV 格式（load_displacement.csv）：
            point_index, total_load_N, max_disp_mm

        PNG 输出（artifacts/load_displacement.png）：
            X 轴 = 最大合位移 [mm]，Y 轴 = 反力合力 [N]
        """
        curve = summary.load_displacement_curve
        if not curve or len(curve) < 2:
            logger.debug("Load-displacement curve: insufficient data, skipping export.")
            return

        # --- CSV ---
        csv_path = output_dir / "load_displacement.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["point_index", "total_load_N", "max_disp_mm"])
            for i, (load, disp) in enumerate(curve):
                writer.writerow([i, load, disp])
        logger.info(f"  load_displacement.csv saved → {csv_path}")

        # --- PNG ---
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            loads = [pt[0] for pt in curve]
            disps = [pt[1] for pt in curve]

            fig, ax = plt.subplots(figsize=(7, 5))
            ax.plot(disps, loads, "o-", color="#2176AE", linewidth=1.8, markersize=5)
            ax.set_xlabel("Max Displacement (mm)")
            ax.set_ylabel("Total Reaction Force (N)")
            ax.set_title("Load–Displacement Curve")
            ax.grid(True, linestyle="--", alpha=0.5)
            plt.tight_layout()

            png_path = output_dir / "artifacts" / "load_displacement.png"
            fig.savefig(str(png_path), dpi=150)
            plt.close(fig)
            logger.info(f"  load_displacement.png saved → {png_path}")
        except Exception as exc:
            logger.warning(f"Load-displacement plot skipped: {exc}")

    def _export_history_csv(self, frd: FRDResult, output_dir: Path) -> None:
        """将位移场数据导出为 CSV 文件（history_data.csv）。

        格式：node_id, U1, U2, U3, ..., U_magnitude
        供外部工具绘制载荷-位移曲线使用。
        """
        u_field = frd.get_field("DISP")
        if u_field is None or not u_field.data:
            return  # 无位移数据时跳过
        csv_path = output_dir / "history_data.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            # 表头：node_id + 各位移分量 + 合位移
            writer.writerow(["node_id"] + u_field.components + ["U_magnitude"])
            for nid in sorted(u_field.data.keys()):
                vals = u_field.data[nid]
                # 合位移 = sqrt(U1² + U2² + U3²)（用前 3 个分量计算）
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
        """将结果 JSON 文件写入运行目录（诊断报告始终写入）。"""
        if summary:
            p = output_dir / "result_summary.json"
            p.write_text(summary.to_json(), encoding="utf-8")
        if manifest:
            p = output_dir / "field_manifest.json"
            p.write_text(manifest.to_json(), encoding="utf-8")
        # 诊断报告无论成功与否都写入
        p = output_dir / "diagnostics.json"
        p.write_text(diagnostics.to_json(), encoding="utf-8")

    def _empty_result(
        self, run_status: RunStatus, model: AnalysisModel, diagnostics: Diagnostics
    ) -> tuple[ResultSummary, FieldManifest, Diagnostics]:
        """构造空的结果三元组（求解失败时返回）。"""
        return (
            ResultSummary(job_id=run_status.job_id, analysis_id=model.metadata.analysis_id),
            FieldManifest(job_id=run_status.job_id, analysis_id=model.metadata.analysis_id),
            diagnostics,
        )
