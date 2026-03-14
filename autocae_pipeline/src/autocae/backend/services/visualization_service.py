"""Visualization service — CAD 几何与 FE 网格可视化（Stage 2 / Stage 3）。

功能：
  - CADVisualizer  : 加载 STEP 文件，按命名面（FIXED_END / LOAD_END / …）着色
  - MeshVisualizer : 解析 mesh.inp，按物理组（Physical Group）着色有限元网格
  - VisualizationService : 对外统一入口，支持交互窗口 + PNG 截图保存

依赖：
  - CadQuery  (CAD STEP 加载 + 面片化)
  - PyVista   (3D 渲染)
  - Gmsh      (可选：重新导入 STEP 获取精确面坐标)
  - matplotlib (可选：网格质量直方图)

使用示例::

    svc = VisualizationService()

    # 仅预览 CAD 几何
    svc.visualize_cad(
        step_file=Path("runs/case_001/model.step"),
        named_faces={"FIXED_END": ..., "LOAD_END": ...},
        output_dir=Path("runs/case_001"),
        interactive=True,
    )

    # 预览 FE 网格
    svc.visualize_mesh(
        mesh_inp_file=Path("runs/case_001/mesh.inp"),
        groups_json=Path("runs/case_001/mesh_groups.json"),
        output_dir=Path("runs/case_001"),
        interactive=True,
    )

    # 一键可视化整个运行目录（CAD + 网格并排）
    svc.visualize_run(run_dir=Path("runs/case_001"), interactive=True)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger


# ---------------------------------------------------------------------------
# 颜色映射：命名面 → RGB（用于 CAD 可视化）
# ---------------------------------------------------------------------------
_FACE_COLORS: dict[str, tuple[float, float, float]] = {
    "FIXED_END":   (0.20, 0.40, 0.80),   # 蓝色  — 固支端
    "LOAD_END":    (0.85, 0.20, 0.20),   # 红色  — 载荷端
    "TOP_FACE":    (0.20, 0.70, 0.30),   # 绿色  — 上面
    "BOTTOM_FACE": (0.90, 0.60, 0.10),   # 橙色  — 下面
    "SOLID":       (0.70, 0.70, 0.70),   # 灰色  — 实体
    "_DEFAULT":    (0.75, 0.75, 0.80),   # 浅灰  — 未命名面
}


# ---------------------------------------------------------------------------
# INP 解析器（CalculiX / Gmsh 格式）
# ---------------------------------------------------------------------------

class InpParser:
    """极简 CalculiX .inp 解析器。

    解析内容：
      - ``*Node``    → node_id → (x, y, z)
      - ``*Element`` → element_id → node_ids,  Elset 名称
      - ``*Elset``   → 集合名 → element_id 列表（generate 关键字暂不支持）
    """

    def __init__(self, inp_path: Path) -> None:
        self.inp_path = Path(inp_path)
        self.nodes: dict[int, np.ndarray] = {}          # node_id → [x, y, z]
        self.elements: dict[int, list[int]] = {}        # elem_id → [node_ids]
        self.elem_type: dict[int, str] = {}             # elem_id → "C3D10" etc.
        self.elsets: dict[str, list[int]] = {}          # set_name → [elem_ids]
        self._elem_to_elset: dict[int, str] = {}        # elem_id → set_name (first match)

    def parse(self) -> "InpParser":
        text = self.inp_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()

        mode = None          # "node" | "element" | "elset"
        current_elset = ""
        current_etype = ""

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("**"):
                continue

            upper = line.upper()

            # ── 关键字行 ──────────────────────────────────────────────────
            if upper.startswith("*NODE"):
                mode = "node"
                continue

            if upper.startswith("*ELEMENT"):
                mode = "element"
                # 提取 type= 和 Elset=
                kw_parts = {
                    p.split("=")[0].strip().upper(): p.split("=")[1].strip()
                    for p in upper.split(",")[1:]
                    if "=" in p
                }
                current_etype = kw_parts.get("TYPE", "UNKNOWN")
                current_elset = kw_parts.get("ELSET", "")
                if current_elset and current_elset not in self.elsets:
                    self.elsets[current_elset] = []
                continue

            if upper.startswith("*ELSET"):
                mode = "elset"
                kw_parts = {
                    p.split("=")[0].strip().upper(): p.split("=")[1].strip()
                    for p in upper.split(",")[1:]
                    if "=" in p
                }
                current_elset = kw_parts.get("ELSET", "_unnamed")
                if current_elset not in self.elsets:
                    self.elsets[current_elset] = []
                continue

            if upper.startswith("*"):
                mode = None
                continue

            # ── 数据行 ────────────────────────────────────────────────────
            if mode == "node":
                parts = line.split(",")
                if len(parts) >= 4:
                    nid = int(parts[0].strip())
                    xyz = np.array([float(parts[1]), float(parts[2]), float(parts[3])])
                    self.nodes[nid] = xyz

            elif mode == "element":
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    eid = int(parts[0])
                    nids = [int(p) for p in parts[1:] if p]
                    self.elements[eid] = nids
                    self.elem_type[eid] = current_etype
                    if current_elset:
                        self.elsets[current_elset].append(eid)
                        if eid not in self._elem_to_elset:
                            self._elem_to_elset[eid] = current_elset

            elif mode == "elset":
                for token in line.split(","):
                    token = token.strip()
                    if token:
                        try:
                            eid = int(token)
                            self.elsets[current_elset].append(eid)
                            if eid not in self._elem_to_elset:
                                self._elem_to_elset[eid] = current_elset
                        except ValueError:
                            pass

        logger.debug(
            f"INP parsed: {len(self.nodes)} nodes, "
            f"{len(self.elements)} elements, "
            f"{len(self.elsets)} elsets"
        )
        return self


# ---------------------------------------------------------------------------
# CAD 可视化器
# ---------------------------------------------------------------------------

class CADVisualizer:
    """将 CadQuery 生成的 STEP 文件渲染为带命名面着色的 3D 视图。

    流程：
      1. 用 CadQuery 加载 STEP
      2. 遍历每个面（BRep Face），面片化（tessellate）成三角网格
      3. 按面中心坐标匹配命名面（与 MeshService 中相同的启发式位置判断）
      4. 使用 PyVista 渲染
    """

    def visualize(
        self,
        step_file: Path,
        bounding_box: dict[str, float] | None = None,
        output_dir: Path | None = None,
        interactive: bool = True,
        save_png: bool = True,
        tessellation_tol: float = 0.05,
    ) -> Path | None:
        """渲染 STEP 文件，返回保存的 PNG 路径（若 save_png=True）。"""
        try:
            import cadquery as cq
            import pyvista as pv
        except ImportError as exc:
            raise RuntimeError(
                "CADVisualizer requires 'cadquery' and 'pyvista'. "
                "Install them: pip install cadquery pyvista"
            ) from exc

        logger.info(f"CADVisualizer: loading STEP → {step_file}")
        compound = cq.importers.importStep(str(step_file))

        # 获取所有面对象（BRep Shell Faces）
        faces = compound.faces().vals()
        logger.info(f"  {len(faces)} BRep faces found")

        # 建立用于命名识别的 bbox 参数
        bbox_params = bounding_box or {}

        # 创建 PyVista plotter
        pv.global_theme.background = "white"
        plotter = pv.Plotter(
            shape=(1, 1),
            title="AutoCAE — CAD Geometry (CadQuery / STEP)",
            off_screen=not interactive,
        )
        plotter.add_axes()
        plotter.show_bounds(
            grid="back",
            location="outer",
            font_size=10,
            xtitle="X (mm)", ytitle="Y (mm)", ztitle="Z (mm)",
        )

        # 为每个 BRep 面面片化并着色
        for face in faces:
            verts, tris = face.tessellate(tessellation_tol)
            if not verts or not tris:
                continue

            v_arr = np.array([[v.x, v.y, v.z] for v in verts], dtype=float)
            # 面中心 → 用于命名匹配
            cx, cy, cz = v_arr.mean(axis=0)

            region_name = self._classify_face(cx, cy, cz, bbox_params)
            color = _FACE_COLORS.get(region_name, _FACE_COLORS["_DEFAULT"])

            # pyvista PolyData：faces array 格式 [3, i, j, k, 3, i, j, k, ...]
            face_arr = np.array([[3, t[0], t[1], t[2]] for t in tris]).flatten()
            poly = pv.PolyData(v_arr, face_arr)
            poly = poly.compute_normals(auto_orient_normals=True)

            plotter.add_mesh(
                poly,
                color=color,
                show_edges=False,
                opacity=0.90,
                label=region_name if region_name != "_DEFAULT" else None,
            )

        # 添加图例（命名面颜色说明）
        def _rgb_hex(r: float, g: float, b: float) -> str:
            return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))

        legend_entries = [
            [name, _rgb_hex(*color)]
            for name, color in _FACE_COLORS.items()
            if name != "_DEFAULT"
        ]
        plotter.add_legend(legend_entries, bcolor="white", face="rectangle", size=(0.18, 0.22))
        plotter.add_title("CAD Geometry — Named Faces", font_size=12)

        png_path: Path | None = None
        if output_dir and save_png:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            png_path = output_dir / "viz_cad.png"
            plotter.screenshot(str(png_path))
            logger.info(f"  CAD screenshot saved → {png_path}")

        if interactive:
            plotter.show()
        else:
            plotter.close()

        return png_path

    @staticmethod
    def _classify_face(
        cx: float, cy: float, cz: float,
        bbox: dict[str, float],
        tol_frac: float = 0.04,
    ) -> str:
        """按面片中心坐标判断所属命名面（与 MeshService 相同的启发式策略）。"""
        if not bbox:
            return "_DEFAULT"

        xmin = bbox.get("xmin", 0.0)
        xmax = bbox.get("xmax", 0.0)
        zmin = bbox.get("zmin", 0.0)
        zmax = bbox.get("zmax", 0.0)

        span_x = abs(xmax - xmin) or 1.0
        span_z = abs(zmax - zmin) or 1.0
        tol_x = span_x * tol_frac
        tol_z = span_z * tol_frac

        if abs(cx - xmin) < tol_x:
            return "FIXED_END"
        if abs(cx - xmax) < tol_x:
            return "LOAD_END"
        if abs(cz - zmax) < tol_z:
            return "TOP_FACE"
        if abs(cz - zmin) < tol_z:
            return "BOTTOM_FACE"
        return "_DEFAULT"


# ---------------------------------------------------------------------------
# 网格可视化器
# ---------------------------------------------------------------------------

class MeshVisualizer:
    """将 mesh.inp（CalculiX 格式）渲染为按物理组着色的有限元网格视图。

    流程：
      1. 解析 mesh.inp → 节点 + 单元 + Elset
      2. 加载 mesh_groups.json → 物理组 ↔ Elset 映射
      3. 用 PyVista 渲染 UnstructuredGrid（表面壳）
      4. 单元按所属物理组着色，未分组单元显示为灰色
    """

    # PyVista 单元类型映射（CalculiX 类型 → VTK Cell Type）
    # VTK 单元类型 ID 参考：https://vtk.org/wp-content/uploads/2015/04/file-formats.pdf
    _VTK_CELL_TYPE: dict[str, int] = {
        "C3D4":  10,   # 4-node tetrahedron
        "C3D10": 24,   # 10-node quadratic tetrahedron
        "C3D8":  12,   # 8-node hexahedron
        "C3D8R": 12,
        "C3D20": 25,   # 20-node quadratic hexahedron
        "C3D20R":25,
        "C3D6":  13,   # 6-node wedge
        "S3":    5,    # 3-node triangle (shell)
        "S4":    9,    # 4-node quad (shell)
        "S4R":   9,
        "S8":    23,   # 8-node quad (shell)
        "S8R":   23,
        "B31":   3,    # 2-node line (beam)
        "B32":   4,    # 3-node line (beam)
    }

    # 物理组颜色映射（与 CAD 命名面一致）
    _GROUP_COLORS: dict[str, tuple[float, float, float]] = {
        "FIXED_END":   (0.20, 0.40, 0.80),
        "LOAD_END":    (0.85, 0.20, 0.20),
        "TOP_FACE":    (0.20, 0.70, 0.30),
        "BOTTOM_FACE": (0.90, 0.60, 0.10),
        "SOLID":       (0.65, 0.65, 0.70),
        "_UNASSIGNED": (0.82, 0.82, 0.85),
    }

    def visualize(
        self,
        mesh_inp_file: Path,
        groups_json: Path | None = None,
        output_dir: Path | None = None,
        interactive: bool = True,
        save_png: bool = True,
        show_edges: bool = True,
        show_quality_histogram: bool = True,
    ) -> Path | None:
        """渲染有限元网格，返回 3D 视图 PNG 路径（若 save_png=True）。

        质量报告另存为 viz_mesh_quality.png（当 show_quality_histogram=True 且 save_png=True 时）。
        """
        try:
            import pyvista as pv
        except ImportError as exc:
            raise RuntimeError("MeshVisualizer requires 'pyvista'. Install: pip install pyvista") from exc

        logger.info(f"MeshVisualizer: parsing {mesh_inp_file}")
        parser = InpParser(mesh_inp_file).parse()

        if not parser.nodes or not parser.elements:
            raise ValueError(f"No nodes/elements found in {mesh_inp_file}")

        # 加载物理组 JSON（可选）
        group_labels = self._load_group_labels(groups_json)

        # 构建 pyvista UnstructuredGrid
        grid = self._build_unstructured_grid(parser)

        # 每个单元附加一个组 ID 标量（用于着色）
        group_names, group_ids = self._build_group_scalar(parser, group_labels)
        grid.cell_data["group_id"] = np.array(group_ids, dtype=np.int32)

        # 构建颜色映射表（LUT）
        color_lut = self._build_color_lut(group_names)

        # ── 3D 网格视图（独立 Plotter 单面板）────────────────────────────
        pv.global_theme.background = "white"
        plotter = pv.Plotter(
            shape=(1, 1),
            title="AutoCAE — FE Mesh (Gmsh / CalculiX .inp)",
            off_screen=not interactive,
        )

        plotter.add_axes()
        plotter.show_bounds(
            grid="back", location="outer", font_size=9,
            xtitle="X (mm)", ytitle="Y (mm)", ztitle="Z (mm)",
        )

        surface = grid.extract_surface()
        plotter.add_mesh(
            surface,
            scalars="group_id",
            cmap=color_lut,
            clim=[0, max(1, len(group_names) - 1)],
            show_edges=show_edges,
            edge_color="black",
            line_width=0.5,
            show_scalar_bar=False,
        )

        # 添加图例（只显示物理组，跳过 _UNASSIGNED）
        legend_entries = [
            [name, self._group_color(name)]
            for name in group_names
            if name != "_UNASSIGNED"
        ]
        if legend_entries:
            plotter.add_legend(legend_entries, bcolor="white", face="rectangle", size=(0.22, 0.28))
        plotter.add_title(
            f"FE Mesh — {len(parser.nodes):,} nodes, {len(parser.elements):,} elements",
            font_size=11,
        )

        png_path: Path | None = None
        if output_dir and save_png:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            png_path = output_dir / "viz_mesh.png"
            plotter.screenshot(str(png_path))
            logger.info(f"  Mesh screenshot saved → {png_path}")

        if interactive:
            plotter.show()
        else:
            plotter.close()

        # ── 质量报告：独立 matplotlib 图（viz_mesh_quality.png）───────────
        if show_quality_histogram and output_dir and save_png:
            quality_report_path = Path(output_dir) / "mesh_quality_report.json"
            self._save_quality_chart(parser, quality_report_path, Path(output_dir))

        return png_path

    # ── 内部方法 ─────────────────────────────────────────────────────────

    def _build_unstructured_grid(self, parser: InpParser) -> Any:
        """从 INP 解析结果构建 pyvista.UnstructuredGrid。"""
        import pyvista as pv

        # 节点数组（按 node_id 排序，建立 id → index 映射）
        sorted_nids = sorted(parser.nodes.keys())
        nid_to_idx = {nid: i for i, nid in enumerate(sorted_nids)}
        points = np.array([parser.nodes[nid] for nid in sorted_nids], dtype=float)

        # 单元数组（pyvista 格式：[n_pts, i0, i1, ..., n_pts, i0, i1, ...]）
        cells: list[int] = []
        cell_types: list[int] = []

        for eid in sorted(parser.elements.keys()):
            nids = parser.elements[eid]
            etype = parser.elem_type.get(eid, "UNKNOWN")
            vtk_type = self._VTK_CELL_TYPE.get(etype)
            if vtk_type is None:
                continue
            indices = [nid_to_idx[n] for n in nids if n in nid_to_idx]
            if not indices:
                continue
            cells.append(len(indices))
            cells.extend(indices)
            cell_types.append(vtk_type)

        if not cells:
            raise ValueError("No supported element types found in INP file.")

        return pv.UnstructuredGrid(
            np.array(cells, dtype=np.int64),
            np.array(cell_types, dtype=np.uint8),
            points,
        )

    def _load_group_labels(self, groups_json: Path | None) -> dict[str, str]:
        """从 mesh_groups.json 加载 Elset → 物理组名称映射。"""
        if groups_json is None or not Path(groups_json).exists():
            return {}
        data = json.loads(Path(groups_json).read_text(encoding="utf-8"))
        mapping: dict[str, str] = {}
        for g in data.get("groups", []):
            set_name = g.get("solver_set_name", "")
            if set_name:
                mapping[set_name.upper()] = set_name
        return mapping

    def _build_group_scalar(
        self, parser: InpParser, group_labels: dict[str, str]
    ) -> tuple[list[str], list[int]]:
        """为每个单元分配一个整数组 ID，返回 (group_name_list, per_element_ids)。

        优先使用 mesh_groups.json 中的命名物理组（FIXED_END / LOAD_END / …）；
        Gmsh 内部自动生成的 SURFACE* / VOLUME* Elset 归入 _UNASSIGNED。
        """
        # 已知物理组名（来自 mesh_groups.json）— 这些才显示在图例中
        known_groups: dict[str, str] = {}  # upper_key → canonical_name
        if group_labels:
            known_groups = {k.upper(): v for k, v in group_labels.items()}
        else:
            # 若没有 JSON，仍接受标准命名（非 SURFACE*/VOLUME* 开头的 Elset）
            _gmsh_auto = ("SURFACE", "VOLUME", "EDGE", "POINT")
            for k in parser.elsets.keys():
                if not any(k.upper().startswith(p) for p in _gmsh_auto):
                    known_groups[k.upper()] = k

        # 构建 group_name 列表（_UNASSIGNED 固定为 0）
        group_names: list[str] = ["_UNASSIGNED"]
        name_to_id: dict[str, int] = {"_UNASSIGNED": 0}
        for canon in known_groups.values():
            if canon not in name_to_id:
                name_to_id[canon] = len(group_names)
                group_names.append(canon)

        # 建立 elem_id → group_id（只处理已知物理组）
        elem_to_group: dict[int, int] = {}
        for upper_key, canon in known_groups.items():
            gid = name_to_id[canon]
            for eid in parser.elsets.get(upper_key, []) + parser.elsets.get(canon, []):
                if eid not in elem_to_group:
                    elem_to_group[eid] = gid

        # 按单元排序输出（与 _build_unstructured_grid 中的 sorted(elements.keys()) 一致）
        valid_etypes = set(self._VTK_CELL_TYPE.keys())
        group_ids: list[int] = []
        for eid in sorted(parser.elements.keys()):
            etype = parser.elem_type.get(eid, "UNKNOWN")
            if etype not in valid_etypes:
                continue
            group_ids.append(elem_to_group.get(eid, 0))

        return group_names, group_ids

    @staticmethod
    def _rgb_to_hex(r: float, g: float, b: float) -> str:
        return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))

    def _build_color_lut(self, group_names: list[str]) -> list[str]:
        """为 pyvista cmap 构建十六进制颜色字符串列表（每个组对应一种颜色）。"""
        fallback_palette = [
            (0.50, 0.50, 0.90), (0.90, 0.50, 0.10), (0.10, 0.70, 0.60),
            (0.70, 0.20, 0.70), (0.20, 0.80, 0.90), (0.80, 0.80, 0.10),
        ]
        lut = []
        for i, name in enumerate(group_names):
            rgb = self._GROUP_COLORS.get(name, fallback_palette[i % len(fallback_palette)])
            lut.append(self._rgb_to_hex(*rgb))
        return lut

    def _group_color(self, name: str) -> str:
        """返回命名物理组的十六进制颜色字符串，未知名称返回默认色。"""
        rgb = self._GROUP_COLORS.get(name, (0.55, 0.55, 0.65))
        return self._rgb_to_hex(*rgb)

    def _save_quality_chart(
        self, parser: InpParser, quality_report_path: Path, output_dir: Path
    ) -> None:
        """将网格质量报告保存为独立 matplotlib PNG（viz_mesh_quality.png）。"""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, axes = plt.subplots(1, 2, figsize=(8, 4))
            fig.suptitle("Mesh Quality Report", fontsize=11, fontweight="bold")

            # 左：单元类型饼图
            etype_counts: dict[str, int] = {}
            for etype in parser.elem_type.values():
                etype_counts[etype] = etype_counts.get(etype, 0) + 1
            if etype_counts:
                axes[0].pie(
                    list(etype_counts.values()),
                    labels=list(etype_counts.keys()),
                    autopct="%1.0f%%",
                    startangle=90,
                )
                axes[0].set_title("Element Types", fontsize=10)

            # 右：质量指标柱图
            if quality_report_path.exists():
                qdata = json.loads(quality_report_path.read_text(encoding="utf-8"))
                metrics = {
                    "Min Quality": qdata.get("min_quality", 0.0),
                    "Avg Quality": qdata.get("avg_quality", 0.0),
                }
                bars = axes[1].bar(
                    list(metrics.keys()),
                    list(metrics.values()),
                    color=["tomato" if v < 0.3 else "steelblue" for v in metrics.values()],
                    width=0.4,
                )
                axes[1].set_ylim(0, 1.05)
                axes[1].axhline(0.3, color="red", linestyle="--", linewidth=1, label="threshold=0.3")
                axes[1].set_title("Mesh Quality (min SICN)", fontsize=10)
                axes[1].legend(fontsize=9)
                for bar, val in zip(bars, metrics.values()):
                    axes[1].text(
                        bar.get_x() + bar.get_width() / 2,
                        val + 0.02, f"{val:.3f}",
                        ha="center", va="bottom", fontsize=9,
                    )
                # 补充文字信息
                n_nodes = qdata.get("node_count", len(parser.nodes))
                n_elem = qdata.get("element_count", len(parser.elements))
                axes[1].set_xlabel(
                    f"Nodes: {n_nodes:,}   Elements: {n_elem:,}", fontsize=8
                )
            else:
                axes[1].text(0.5, 0.5, "quality_report.json not found",
                             ha="center", va="center", transform=axes[1].transAxes)
                axes[1].axis("off")

            plt.tight_layout()
            out_path = output_dir / "viz_mesh_quality.png"
            plt.savefig(str(out_path), dpi=150, bbox_inches="tight")
            plt.close(fig)
            logger.info(f"  Quality chart saved → {out_path}")

        except Exception as exc:
            logger.warning(f"Quality chart rendering skipped: {exc}")


# ---------------------------------------------------------------------------
# CalculiX 结果场可视化器
# ---------------------------------------------------------------------------

class ResultsVisualizer:
    """将 CalculiX job.frd 的位移场和 von Mises 应力场渲染为彩色云图。

    流程：
      1. 用 InpParser 解析 mesh.inp 获取网格拓扑
      2. 用 FRDParser 解析 job.frd 获取 DISP / STRESS 节点数据
      3. 构建 pyvista.UnstructuredGrid，按节点 ID 顺序注入场量数组
      4. 分别渲染位移云图（viridis）和 Mises 应力云图（plasma）
    """

    def visualize(
        self,
        frd_file: Path,
        mesh_inp_file: Path,
        output_dir: Path | None = None,
        interactive: bool = True,
        save_png: bool = True,
    ) -> dict[str, Path | None]:
        """渲染 CalculiX 结果场，返回 {"displacement": png_path, "stress": png_path}。"""
        try:
            import pyvista as pv
        except ImportError as exc:
            raise RuntimeError("ResultsVisualizer requires 'pyvista'. Install: pip install pyvista") from exc

        # 延迟导入，避免循环依赖
        from autocae.backend.services.postprocess_service import FRDParser

        logger.info(f"ResultsVisualizer: parsing mesh → {mesh_inp_file}")
        inp_parser = InpParser(mesh_inp_file).parse()
        if not inp_parser.nodes or not inp_parser.elements:
            raise ValueError(f"No nodes/elements in {mesh_inp_file}")

        logger.info(f"ResultsVisualizer: parsing FRD → {frd_file}")
        frd_result = FRDParser().parse(frd_file)

        # 节点 ID 顺序（与 _build_unstructured_grid 保持一致）
        sorted_nids = sorted(inp_parser.nodes.keys())
        nid_to_idx = {nid: i for i, nid in enumerate(sorted_nids)}
        n_pts = len(sorted_nids)

        # 构建 UnstructuredGrid（复用 MeshVisualizer 的方法）
        grid = MeshVisualizer()._build_unstructured_grid(inp_parser)

        output_paths: dict[str, Path | None] = {"displacement": None, "stress": None}

        # ── 位移场 ────────────────────────────────────────────────────────
        disp_field = frd_result.get_field("DISP")
        if disp_field and disp_field.data:
            disp_mag = np.zeros(n_pts)
            for nid, vals in disp_field.data.items():
                idx = nid_to_idx.get(nid)
                if idx is not None and len(vals) >= 3:
                    disp_mag[idx] = float(np.sqrt(vals[0]**2 + vals[1]**2 + vals[2]**2))
            grid.point_data["displacement_mm"] = disp_mag

            pv.global_theme.background = "white"
            pl = pv.Plotter(off_screen=not interactive, title="AutoCAE — Displacement Field")
            pl.add_axes()
            pl.show_bounds(grid="back", location="outer", font_size=9,
                           xtitle="X (mm)", ytitle="Y (mm)", ztitle="Z (mm)")
            pl.add_mesh(
                grid,
                scalars="displacement_mm",
                cmap="viridis",
                show_edges=False,
                scalar_bar_args={"title": "Displacement Magnitude (mm)", "fmt": "%.3e"},
            )
            pl.add_title("Displacement Magnitude — Flat Plate Tension", font_size=11)

            if output_dir and save_png:
                out = Path(output_dir) / "viz_results_displacement.png"
                pl.screenshot(str(out))
                output_paths["displacement"] = out
                logger.info(f"  Displacement screenshot saved → {out}")

            if interactive:
                pl.show()
            else:
                pl.close()
        else:
            logger.warning("DISP field not found in FRD — skipping displacement visualization")

        # ── Mises 应力场 ──────────────────────────────────────────────────
        stress_field = frd_result.get_field("STRESS")
        if stress_field and stress_field.data:
            mises = np.zeros(n_pts)
            # 检查是否有直接输出的 Mises 分量（'V' 列）
            mises_idx = next(
                (i for i, c in enumerate(stress_field.components)
                 if "MISES" in c.upper() or c.upper() == "V"),
                None,
            )
            for nid, vals in stress_field.data.items():
                idx = nid_to_idx.get(nid)
                if idx is None:
                    continue
                if mises_idx is not None and mises_idx < len(vals):
                    mises[idx] = abs(vals[mises_idx])
                elif len(vals) >= 6:
                    sx, sy, sz, sxy, sxz, syz = vals[0], vals[1], vals[2], vals[3], vals[4], vals[5]
                    mises[idx] = float(np.sqrt(0.5 * (
                        (sx - sy)**2 + (sy - sz)**2 + (sz - sx)**2
                        + 6 * (sxy**2 + sxz**2 + syz**2)
                    )))
            grid.point_data["mises_stress_MPa"] = mises

            pv.global_theme.background = "white"
            pl = pv.Plotter(off_screen=not interactive, title="AutoCAE — von Mises Stress Field")
            pl.add_axes()
            pl.show_bounds(grid="back", location="outer", font_size=9,
                           xtitle="X (mm)", ytitle="Y (mm)", ztitle="Z (mm)")
            pl.add_mesh(
                grid,
                scalars="mises_stress_MPa",
                cmap="plasma",
                show_edges=False,
                scalar_bar_args={"title": "von Mises Stress (MPa)", "fmt": "%.3e"},
            )
            pl.add_title("von Mises Stress — Flat Plate Tension", font_size=11)

            if output_dir and save_png:
                out = Path(output_dir) / "viz_results_stress.png"
                pl.screenshot(str(out))
                output_paths["stress"] = out
                logger.info(f"  Stress screenshot saved → {out}")

            if interactive:
                pl.show()
            else:
                pl.close()
        else:
            logger.warning("STRESS field not found in FRD — skipping stress visualization")

        return output_paths


# ---------------------------------------------------------------------------
# 对外统一入口
# ---------------------------------------------------------------------------

class VisualizationService:
    """AutoCAE 可视化服务对外统一入口。

    提供三种调用方式：
      1. visualize_cad()  — 仅 CAD 几何（STEP 文件）
      2. visualize_mesh() — 仅 FE 网格（mesh.inp）
      3. visualize_run()  — 从运行目录自动找文件，并排显示 CAD + 网格
    """

    def __init__(self) -> None:
        self._cad_viz     = CADVisualizer()
        self._mesh_viz    = MeshVisualizer()
        self._results_viz = ResultsVisualizer()

    def visualize_cad(
        self,
        step_file: Path,
        bounding_box: dict[str, float] | None = None,
        output_dir: Path | None = None,
        interactive: bool = True,
        save_png: bool = True,
    ) -> Path | None:
        """渲染 STEP 几何文件（Stage 2 输出）。"""
        return self._cad_viz.visualize(
            step_file=step_file,
            bounding_box=bounding_box,
            output_dir=output_dir,
            interactive=interactive,
            save_png=save_png,
        )

    def visualize_mesh(
        self,
        mesh_inp_file: Path,
        groups_json: Path | None = None,
        output_dir: Path | None = None,
        interactive: bool = True,
        save_png: bool = True,
    ) -> Path | None:
        """渲染有限元网格（Stage 3 输出）。"""
        return self._mesh_viz.visualize(
            mesh_inp_file=mesh_inp_file,
            groups_json=groups_json,
            output_dir=output_dir,
            interactive=interactive,
            save_png=save_png,
        )

    def visualize_run(
        self,
        run_dir: Path,
        interactive: bool = True,
        save_png: bool = True,
    ) -> dict[str, Path | None]:
        """从运行目录自动定位文件，依次可视化 CAD + 网格 + CalculiX 结果场。

        返回 {"cad": ..., "mesh": ..., "results_displacement": ..., "results_stress": ...}。
        """
        run_dir = Path(run_dir)
        results: dict[str, Path | None] = {
            "cad": None,
            "mesh": None,
            "results_displacement": None,
            "results_stress": None,
        }

        # ── Stage 2 输出：model.step ──────────────────────────────────────
        step_file = run_dir / "model.step"
        geo_meta_file = run_dir / "geometry_meta.json"

        if step_file.exists():
            bbox: dict[str, float] = {}
            if geo_meta_file.exists():
                meta = json.loads(geo_meta_file.read_text(encoding="utf-8"))
                bbox = meta.get("bounding_box") or {}
            try:
                results["cad"] = self.visualize_cad(
                    step_file=step_file,
                    bounding_box=bbox,
                    output_dir=run_dir,
                    interactive=interactive,
                    save_png=save_png,
                )
            except Exception as exc:
                logger.warning(f"CAD visualization failed: {exc}")
        else:
            logger.warning(f"model.step not found in {run_dir} — skipping CAD visualization")

        # ── Stage 3 输出：mesh.inp ────────────────────────────────────────
        mesh_file = run_dir / "mesh.inp"
        groups_file = run_dir / "mesh_groups.json"

        if mesh_file.exists():
            try:
                results["mesh"] = self.visualize_mesh(
                    mesh_inp_file=mesh_file,
                    groups_json=groups_file if groups_file.exists() else None,
                    output_dir=run_dir,
                    interactive=interactive,
                    save_png=save_png,
                )
            except Exception as exc:
                logger.warning(f"Mesh visualization failed: {exc}")
        else:
            logger.warning(f"mesh.inp not found in {run_dir} — skipping mesh visualization")

        # ── Stage 6/7 输出：job.frd（CalculiX 结果场）────────────────────
        frd_file = run_dir / "job.frd"
        if frd_file.exists() and mesh_file.exists():
            try:
                res = self._results_viz.visualize(
                    frd_file=frd_file,
                    mesh_inp_file=mesh_file,
                    output_dir=run_dir,
                    interactive=interactive,
                    save_png=save_png,
                )
                results["results_displacement"] = res.get("displacement")
                results["results_stress"] = res.get("stress")
            except Exception as exc:
                logger.warning(f"Results visualization failed: {exc}")
        else:
            logger.warning(f"job.frd not found in {run_dir} — skipping results visualization")

        return results
