"""圆柱壳（Cylindrical Shell）CAD 模板。

薄壁中空圆柱体，用于内压、轴压屈曲等壳体结构分析。
几何参数：
    R    — 中面半径（radius）[mm]；若未指定，取 width/2
    t    — 壁厚（wall_thickness）[mm]；若未指定，取 thickness
    L    — 圆柱轴向长度（length）[mm]
约束：t < R（否则已不是薄壁壳）
"""

from __future__ import annotations

import math
from pathlib import Path

from loguru import logger

from autocae.backend.templates.cad.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class CylindricalShellTemplate(BaseCADTemplate):
    """薄壁圆柱壳模板（中空圆筒）。"""

    geometry_type = "cylindrical_shell"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        """构建圆柱壳几何并导出 STEP 文件。

        建模思路：
            在 YZ 平面上绘制两个同心圆（外圆 outer_r、内圆 inner_r），
            然后沿 X 方向拉伸 L，最后平移使轴向中心位于 X=0。
        """
        import cadquery as cq

        geo = spec.geometry
        L = geo.length                               # 轴向长度 [mm]
        R = geo.extra.get("radius", geo.width / 2.0)         # 中面半径 [mm]
        t = geo.extra.get("wall_thickness", geo.thickness)   # 壁厚 [mm]

        logger.info(f"Building cylindrical_shell: L={L} R={R} t={t} mm")

        # 几何有效性检查：壁厚必须小于中面半径
        if t >= R:
            raise ValueError(
                f"wall_thickness ({t} mm) must be less than radius ({R} mm)."
            )

        outer_r = R + t / 2.0  # 外半径 = 中面半径 + 半壁厚
        inner_r = R - t / 2.0  # 内半径 = 中面半径 - 半壁厚

        # 双圆截面拉伸（CadQuery 双 circle → extrude 自动形成环形截面）
        shell = (
            cq.Workplane("YZ")
            .circle(outer_r)
            .circle(inner_r)
            .extrude(L)
            .translate((-L / 2.0, 0, 0))  # 平移使轴向中心在 X=0
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        cq.exporters.export(shell, str(step_path))
        logger.info(f"  STEP exported → {step_path}")

        geometry_meta = GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces={},
            named_edges={},
            bounding_box={
                "xmin": -L / 2, "xmax": L / 2,
                "ymin": -outer_r, "ymax": outer_r,
                "zmin": -outer_r, "zmax": outer_r,
            },
            # 环形截面体积 = π(外径² - 内径²) × L
            volume=math.pi * (outer_r ** 2 - inner_r ** 2) * L,
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "END_A":         "Annular face at X = -L/2",  # 端面 A（固定端）
                "END_B":         "Annular face at X = +L/2",  # 端面 B（加载端）
                "OUTER_SURFACE": "Outer cylindrical surface",  # 外圆柱面（施加外压/约束）
                "INNER_SURFACE": "Inner cylindrical surface",  # 内圆柱面（施加内压）
            },
        )
