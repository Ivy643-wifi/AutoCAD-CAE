"""长桁加筋壁板（Stringer-Stiffened Panel）CAD 模板。

航空典型结构：蒙皮平板 + 等间距刀形长桁（Blade Stringer）组合。
几何参数：
    L, W, Ts         — 蒙皮长度、宽度、厚度 [mm]
    n_stringers      — 长桁根数（默认 3）
    stringer_height  — 长桁高度 Sh（默认 5×Ts）[mm]
    stringer_width   — 长桁腹板宽度 Sw（默认 2×Ts）[mm]

建模规则：
    长桁均匀分布在蒙皮宽度方向，间距 = W/(n+1)。
    每根长桁从蒙皮顶面（Z=Ts）向上延伸 Sh。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.backend.templates.cad.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class StringerStiffenedPanelTemplate(BaseCADTemplate):
    """蒙皮平板 + 刀形长桁的加筋壁板模板。"""

    geometry_type = "stringer_stiffened_panel"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        """构建加筋壁板几何并导出 STEP 文件。

        建模步骤：
            1. 建蒙皮底板（XY 平面，Z 从 0 到 Ts）
            2. 按等间距在 Y 方向排布 n 根刀形长桁（从 Z=Ts 开始向上）
            3. 将所有长桁与蒙皮做布尔并集（union）
        """
        import cadquery as cq

        geo = spec.geometry
        L  = geo.length     # 壁板长度（X 方向）[mm]
        W  = geo.width      # 壁板宽度（Y 方向）[mm]
        Ts = geo.thickness  # 蒙皮厚度（Z 方向）[mm]

        n  = int(geo.extra.get("n_stringers", 3))           # 长桁根数
        Sh = geo.extra.get("stringer_height", Ts * 5.0)     # 长桁高度 [mm]
        Sw = geo.extra.get("stringer_width",  Ts * 2.0)     # 长桁腹板宽度 [mm]

        logger.info(
            f"Building stringer_stiffened_panel: L={L} W={W} Ts={Ts} "
            f"n_stringers={n} Sh={Sh} Sw={Sw} mm"
        )

        # 步骤 1：建蒙皮底板（Z 从 0 开始，不居中）
        panel = cq.Workplane("XY").box(L, W, Ts, centered=(True, True, False))

        # 步骤 2 & 3：按等间距排布并合并每根长桁
        if n > 0:
            pitch = W / (n + 1)  # 长桁间距 [mm]
            for i in range(1, n + 1):
                y_pos = -W / 2 + pitch * i  # 当前长桁的 Y 坐标
                stringer = (
                    cq.Workplane("XY")
                    .box(L, Sw, Sh, centered=(True, True, False))
                    .translate((0.0, y_pos, Ts))  # 从蒙皮顶面向上生长
                )
                panel = panel.union(stringer)  # 布尔并集

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        cq.exporters.export(panel, str(step_path))
        logger.info(f"  STEP exported → {step_path}")

        total_height = Ts + Sh  # 结构总高度（蒙皮厚度 + 长桁高度）[mm]
        geometry_meta = GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces={},
            named_edges={},
            bounding_box={
                "xmin": -L / 2, "xmax": L / 2,
                "ymin": -W / 2, "ymax": W / 2,
                "zmin": 0.0,    "zmax": total_height,
            },
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "FIXED_END":   "Panel cross-section face at X = -L/2",  # 固定端截面
                "LOAD_END":    "Panel cross-section face at X = +L/2",  # 加载端截面（施加压缩或拉伸）
                "SKIN_BOTTOM": "Bottom face of skin at Z = 0",           # 蒙皮底面（Z=0）
            },
        )
