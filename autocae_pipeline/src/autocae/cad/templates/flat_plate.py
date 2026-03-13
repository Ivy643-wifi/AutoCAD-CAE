"""平板（Flat Plate）CAD 模板。

支持的分析类型：拉伸、压缩、弯曲、屈曲。

几何描述：
    基础几何：长 × 宽 × 厚 的矩形实体板
        - X 方向 = 加载方向（length）
        - Y 方向 = 宽度方向（width）
        - Z 方向 = 厚度方向（thickness）
        - 几何中心在原点

    可选特征：圆孔（Feature: HOLE）
        - 孔位于板的几何中心
        - 孔径从 Feature.params["diameter"] 读取，默认 0.2×width

命名面（用于边界条件和载荷施加）：
    FIXED_END   → X = -L/2 的面（固定端，施加边界条件）
    LOAD_END    → X = +L/2 的面（加载端，施加拉/压载荷）
    TOP_FACE    → Z = +T/2 的面（上表面，施加弯曲载荷）
    BOTTOM_FACE → Z = -T/2 的面（下表面）
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.cad.templates.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec, FeatureName
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class FlatPlateTemplate(BaseCADTemplate):
    """矩形平板模板（可选开孔特征）。

    对应 GeometryType.FLAT_PLATE 和 GeometryType.NOTCHED_PLATE（复用）。
    """

    geometry_type = "flat_plate"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        """构建平板几何并导出 STEP。

        步骤：
            1. 用 CadQuery 创建 L×W×T 的矩形实体
            2. 若有 HOLE 特征，在中心挖圆孔
            3. 导出 STEP 文件
            4. 构造 GeometryMeta（包围盒已知，命名面 tag 留空待 Gmsh 填充）

        Args:
            spec:       CaseSpec（读取 geometry.length/width/thickness 和 features）
            output_dir: 运行目录

        Returns:
            CADResult（step_file + geometry_meta + named_faces 描述）
        """
        import cadquery as cq

        geo = spec.geometry
        L = geo.length      # 加载方向长度 [mm]，X 轴
        W = geo.width       # 宽度 [mm]，Y 轴
        T = geo.thickness   # 厚度 [mm]，Z 轴

        logger.info(
            f"Building flat_plate: L={L} W={W} T={T} mm"
        )

        # 以原点为中心创建矩形实体板
        # centered=(True, True, True) 表示几何中心在坐标原点
        plate = (
            cq.Workplane("XY")
            .box(L, W, T, centered=(True, True, True))
        )

        # 检查是否需要添加孔特征
        hole_feature = next(
            (f for f in spec.features if f.name == FeatureName.HOLE and f.enabled), None
        )
        if hole_feature:
            # 从特征参数读取孔径，默认为宽度的 20%
            d = hole_feature.params.get("diameter", W * 0.2)
            logger.info(f"  Adding hole: diameter={d} mm (centre of plate)")
            # 在上表面（>Z）工作平面上挖通孔
            plate = plate.faces(">Z").workplane().hole(d)

        # 导出 STEP 文件（G-03：几何交换格式唯一为 STEP）
        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        cq.exporters.export(plate, str(step_path))
        logger.info(f"  STEP exported → {step_path}")

        # 命名面提示（字符串描述，供调试；Gmsh 用包围盒位置重新映射）
        named_faces_hints = {
            "FIXED_END": "Face at X = -{L/2} (clamped boundary)",
            "LOAD_END":  "Face at X = +{L/2} (applied load)",
            "TOP_FACE":  "Face at Z = +{T/2}",
            "BOTTOM_FACE": "Face at Z = -{T/2}",
        }

        # 构造 GeometryMeta：包围盒已知（以原点为中心）
        # named_faces 留空 {}，MeshBuilder 的 _assign_physical_groups 会填充
        geometry_meta = GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces={},   # 由 MeshBuilder 根据 bounding_box 填充
            named_edges={},
            bounding_box={
                "xmin": -L / 2, "xmax": L / 2,
                "ymin": -W / 2, "ymax": W / 2,
                "zmin": -T / 2, "zmax": T / 2,
            },
            volume=L * W * T,  # 理论体积（不含孔）
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces=named_faces_hints,
        )
