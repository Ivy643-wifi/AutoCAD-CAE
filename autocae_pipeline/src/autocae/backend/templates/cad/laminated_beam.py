"""层合梁（Laminated Beam）CAD 模板。

实心矩形截面梁，用于悬臂弯曲、扭转等梁结构分析。
几何建模：在 YZ 平面绘制矩形截面（W × T），沿 X 方向拉伸 L。
命名面：
    FIXED_END   — 悬臂固定端（X = -L/2）
    FREE_END    — 悬臂自由端（X = +L/2）
    TOP_FLANGE  — 上翼缘面（Z = +T/2）
    BOTTOM_FLANGE — 下翼缘面（Z = -T/2）
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.backend.templates.cad.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class LaminatedBeamTemplate(BaseCADTemplate):
    """实心矩形截面层合梁模板。"""

    geometry_type = "laminated_beam"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        """构建矩形截面梁几何并导出 STEP 文件。

        建模思路：
            在 YZ 平面绘制 W×T 矩形，沿 +X 拉伸 L，
            再平移 -L/2 使几何中心在原点（X=0）。
        """
        import cadquery as cq

        geo = spec.geometry
        L = geo.length    # 梁长（X 方向）[mm]
        W = geo.width     # 截面宽度（Y 方向）[mm]
        T = geo.thickness # 截面高度（Z 方向）[mm]

        logger.info(f"Building laminated_beam: L={L} W={W} T={T} mm")

        # YZ 平面矩形截面 → 沿 X 方向拉伸 → 轴向平移对中
        beam = (
            cq.Workplane("YZ")
            .rect(W, T)
            .extrude(L)
            .translate((-L / 2.0, 0, 0))
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        cq.exporters.export(beam, str(step_path))
        logger.info(f"  STEP exported → {step_path}")

        geometry_meta = GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces={},
            named_edges={},
            bounding_box={
                "xmin": -L / 2, "xmax": L / 2,
                "ymin": -W / 2, "ymax": W / 2,
                "zmin": -T / 2, "zmax": T / 2,
            },
            volume=L * W * T,  # 实心矩形截面体积
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "FIXED_END":      "Cross-section face at X = -L/2",  # 悬臂固定端截面
                "FREE_END":       "Cross-section face at X = +L/2",  # 悬臂自由端截面（施加载荷）
                "TOP_FLANGE":     "Face at Z = +T/2",                # 上翼缘（受弯时受拉/压）
                "BOTTOM_FLANGE":  "Face at Z = -T/2",                # 下翼缘
            },
        )
