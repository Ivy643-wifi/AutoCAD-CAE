"""开孔平板（Open-Hole Plate）CAD 模板。

标准试验件：矩形平板中央带贯穿圆孔（OHT/OHC 强度试验件）。
开孔比 d/W 通常为 0.1~0.3（过大会与侧边连通，几何无效）。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.backend.templates.cad.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class OpenHolePlateTemplate(BaseCADTemplate):
    """中央贯穿圆孔的平板模板（标准 OHT/OHC 试验件几何）。"""

    geometry_type = "open_hole_plate"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        """构建开孔平板几何并导出 STEP 文件。

        参数说明：
            spec.geometry.length       — 板长 L（X 方向，加载方向）[mm]
            spec.geometry.width        — 板宽 W（Y 方向）[mm]
            spec.geometry.thickness    — 板厚 T（Z 方向）[mm]
            spec.geometry.extra['hole_diameter'] — 孔直径 d（默认 0.2W）[mm]
        """
        import cadquery as cq

        geo = spec.geometry
        L = geo.length    # 板长（X 方向，加载方向）[mm]
        W = geo.width     # 板宽（Y 方向）[mm]
        T = geo.thickness # 板厚（Z 方向）[mm]
        d = geo.extra.get("hole_diameter", W * 0.2)  # 孔直径，默认 0.2W [mm]

        # 几何有效性检查：孔直径不能超过板宽
        if d >= W:
            raise ValueError(
                f"hole_diameter ({d} mm) must be smaller than plate width ({W} mm)."
            )

        logger.info(f"Building open_hole_plate: L={L} W={W} T={T} hole_d={d} mm")

        # 先建矩形实体，再在顶面中心打贯穿孔
        plate = (
            cq.Workplane("XY")
            .box(L, W, T, centered=(True, True, True))
            .faces(">Z")
            .workplane()
            .hole(d)
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        cq.exporters.export(plate, str(step_path))
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
            # 实际体积 = 实体体积 - 圆孔体积（用 π ≈ 3.14159）
            volume=L * W * T - 3.14159 * (d / 2) ** 2 * T,
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "FIXED_END":    "Face at X = -L/2",      # 固定端（夹具夹持，施加固支约束）
                "LOAD_END":     "Face at X = +L/2",      # 加载端（施加拉/压载荷）
                "HOLE_SURFACE": "Cylindrical surface of the hole",  # 孔壁柱面（应力集中区）
            },
        )
