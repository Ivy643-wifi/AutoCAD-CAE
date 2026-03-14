"""螺接搭接接头（Bolted Lap Joint）CAD 模板。

单剪搭接构型：板 A 从左侧延伸，板 B 从右侧延伸，两板在搭接区（overlap）重叠，
通过 n 个螺栓孔模拟紧固件连接。

几何参数：
    L, W, T       — 接头总长、宽度、单板厚度 [mm]
    overlap_length（Lo）— 搭接长度（默认 0.2L）[mm]
    n_bolts       — 螺栓数量（默认 2）
    bolt_diameter（Db）— 螺栓孔直径（默认 2T）[mm]
    bolt_pitch    — 螺栓间距（Y 方向，默认 W/(n+1)）[mm]

Z 方向位置：
    板 A（下板）：Z 从 -T/2 到 +T/2
    板 B（上板）：Z 从  T/2 到  3T/2
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.backend.templates.cad.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class BoltedLapJointTemplate(BaseCADTemplate):
    """两板单剪螺接搭接接头模板。"""

    geometry_type = "bolted_lap_joint"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        """构建螺接搭接接头几何并导出 STEP 文件。

        建模步骤：
            1. 建板 A（下板，从 X=-L/2 延伸至搭接区右边界）
            2. 建板 B（上板，从搭接区左边界延伸至 X=+L/2，Z 方向错开一个板厚）
            3. 在搭接区打穿两块板的螺栓孔（沿 Y 方向均匀排列）
            4. 布尔合并后导出 STEP
        """
        import cadquery as cq

        geo = spec.geometry
        L   = geo.length   # 接头总长 [mm]
        W   = geo.width    # 接头宽度 [mm]
        T   = geo.thickness  # 单板厚度 [mm]
        Lo  = geo.extra.get("overlap_length", L * 0.2)    # 搭接长度 [mm]
        n   = int(geo.extra.get("n_bolts", 2))            # 螺栓数量
        Db  = geo.extra.get("bolt_diameter", T * 2.0)     # 螺栓孔直径 [mm]
        pitch = geo.extra.get("bolt_pitch", W / (n + 1))  # 螺栓间距（Y 方向）[mm]

        logger.info(
            f"Building bolted_lap_joint: L={L} W={W} T={T} "
            f"overlap={Lo} n_bolts={n} bolt_d={Db} mm"
        )

        # 步骤 1：板 A（下板），从 X=-L/2 开始，长度 = L/2+Lo/2，Z 居中于 0
        plate_a = cq.Workplane("XY").box(
            L / 2 + Lo / 2, W, T, centered=(False, True, True)
        ).translate((-L / 2, 0, 0))

        # 步骤 2：板 B（上板），从搭接区左边界开始，Z 方向抬高一个板厚 T
        plate_b = cq.Workplane("XY").box(
            L / 2 + Lo / 2, W, T, centered=(False, True, True)
        ).translate((-Lo / 2, 0, T))

        # 步骤 3：在搭接区打螺栓孔（沿 Y 均匀排列）
        for i in range(1, n + 1):
            y_pos = -W / 2 + pitch * i  # 当前螺栓的 Y 坐标
            plate_a = (
                plate_a.faces(">Z").workplane()
                .center(0, y_pos).hole(Db, T)  # 穿透板 A
            )
            plate_b = (
                plate_b.faces(">Z").workplane()
                .center(0, y_pos).hole(Db, T)  # 穿透板 B
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        # 步骤 4：布尔合并两块板并导出
        compound = plate_a.val().fuse(plate_b.val())
        compound_wp = cq.Workplane().newObject([compound])
        cq.exporters.export(compound_wp, str(step_path))
        logger.info(f"  STEP exported → {step_path}")

        geometry_meta = GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces={},
            named_edges={},
            bounding_box={
                "xmin": -L / 2, "xmax": L / 2,
                "ymin": -W / 2, "ymax": W / 2,
                "zmin": -T / 2, "zmax": T * 3 / 2,  # 两板叠加后 Z 总高 = 2T
            },
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "PLATE_A_GRIP": "Free end of plate A (X = -L/2)",  # 板 A 自由端（夹具夹持/固定端）
                "PLATE_B_GRIP": "Free end of plate B (X = +L/2)",  # 板 B 自由端（加载端）
                "BOLT_HOLES":   "Cylindrical bore surfaces of bolt holes",  # 螺栓孔壁面（接触/绑接区）
            },
        )
