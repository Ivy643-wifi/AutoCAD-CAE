"""夹芯板（Sandwich Plate）CAD 模板。

三层结构：上面板 + 芯材 + 下面板（航空典型轻质结构）。
几何参数：
    L, W            — 板长、板宽 [mm]
    facesheet_thickness（Tf）— 单层面板厚度（默认 = geometry.thickness）[mm]
    core_thickness（Tc）    — 芯材厚度（默认 = 8×Tf）[mm]

Z 方向叠层顺序（从 Z=0 开始）：
    [0, Tf]         → 下面板
    [Tf, Tf+Tc]     → 芯材
    [Tf+Tc, 2Tf+Tc] → 上面板
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.backend.templates.cad.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class SandwichPlateTemplate(BaseCADTemplate):
    """上下面板 + 芯材组成的夹芯板模板。"""

    geometry_type = "sandwich_plate"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        """构建夹芯板三层几何并导出 STEP 文件。

        建模步骤：
            1. 建下面板（Z: 0 → Tf）
            2. 建芯材（Z: Tf → Tf+Tc）
            3. 建上面板（Z: Tf+Tc → 2Tf+Tc）
            4. 三者布尔并集 → 一体化 STEP 几何
        """
        import cadquery as cq

        geo = spec.geometry
        L  = geo.length   # 板长（X 方向）[mm]
        W  = geo.width    # 板宽（Y 方向）[mm]
        Tf = geo.extra.get("facesheet_thickness", geo.thickness)   # 面板厚度 [mm]
        Tc = geo.extra.get("core_thickness", geo.thickness * 8.0)  # 芯材厚度 [mm]

        total_T = 2 * Tf + Tc  # 三层总厚度 [mm]

        logger.info(
            f"Building sandwich_plate: L={L} W={W} "
            f"Tf={Tf} Tc={Tc} total_T={total_T} mm"
        )

        # 步骤 1：下面板（从 Z=0 开始，不居中）
        bottom = cq.Workplane("XY").box(L, W, Tf, centered=(True, True, False))
        # 步骤 2：芯材（从 Z=Tf 开始）
        core = (
            cq.Workplane("XY")
            .box(L, W, Tc, centered=(True, True, False))
            .translate((0.0, 0.0, Tf))
        )
        # 步骤 3：上面板（从 Z=Tf+Tc 开始）
        top = (
            cq.Workplane("XY")
            .box(L, W, Tf, centered=(True, True, False))
            .translate((0.0, 0.0, Tf + Tc))
        )
        # 步骤 4：布尔并集合并三层
        panel = bottom.union(core).union(top)

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        cq.exporters.export(panel, str(step_path))
        logger.info(f"  STEP exported → {step_path}")

        geometry_meta = GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces={},
            named_edges={},
            bounding_box={
                "xmin": -L / 2, "xmax": L / 2,
                "ymin": -W / 2, "ymax": W / 2,
                "zmin": 0.0,    "zmax": total_T,
            },
            volume=L * W * total_T,  # 三层总体积（近似，忽略界面）
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "FIXED_END":   "Cross-section at X = -L/2",          # 固定端截面
                "LOAD_END":    "Cross-section at X = +L/2",           # 加载端截面（弯曲/剪切）
                "TOP_FACE":    "Top face of upper face-sheet",         # 上面板顶面（受压/受拉）
                "BOTTOM_FACE": "Bottom face of lower face-sheet",      # 下面板底面（受拉/受压）
            },
        )
