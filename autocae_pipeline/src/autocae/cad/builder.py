"""CAD Builder — 根据 GeometryType 分派到对应的 CAD 模板。

职责：
    接收 CaseSpec，查找对应的 CAD 模板实例，调用其 build() 方法，
    生成 model.step 和 geometry_meta.json。

设计原则：
    G-02：CAD 双轨制（CadQuery 主轨 + 外部 STEP 备轨）。
    G-03：几何交换格式唯一为 STEP。
    本模块只处理"主轨"（CadQuery 生成），外部 STEP 路径需另外处理。

扩展点：
    在 _TEMPLATE_REGISTRY 字典中添加新的 GeometryType → BaseCADTemplate 映射，
    即可支持新的结构族，无需修改 CADBuilder 的其他逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from autocae.cad.templates.base import BaseCADTemplate, CADResult
from autocae.cad.templates.flat_plate import FlatPlateTemplate
from autocae.cad.templates.open_hole_plate import OpenHolePlateTemplate
from autocae.cad.templates.cylindrical_shell import CylindricalShellTemplate
from autocae.cad.templates.laminated_beam import LaminatedBeamTemplate
from autocae.cad.templates.stringer_stiffened_panel import StringerStiffenedPanelTemplate
from autocae.cad.templates.sandwich_plate import SandwichPlateTemplate
from autocae.cad.templates.bolted_lap_joint import BoltedLapJointTemplate
from autocae.schemas.case_spec import CaseSpec, GeometryType
from autocae.schemas.mesh import GeometryMeta


# ---------------------------------------------------------------------------
# CAD 模板注册表：GeometryType → 模板实例（单例，复用）
# ---------------------------------------------------------------------------
# 注意：NOTCHED_PLATE 复用 FlatPlateTemplate（通过 Feature.CUTOUT 实现缺口）
#       PRESSURE_SHELL 复用 CylindricalShellTemplate（只是边界条件不同）
_TEMPLATE_REGISTRY: dict[GeometryType, BaseCADTemplate] = {
    GeometryType.FLAT_PLATE:               FlatPlateTemplate(),
    GeometryType.OPEN_HOLE_PLATE:          OpenHolePlateTemplate(),
    GeometryType.NOTCHED_PLATE:            FlatPlateTemplate(),   # 缺口板复用平板模板
    GeometryType.CYLINDRICAL_SHELL:        CylindricalShellTemplate(),
    GeometryType.PRESSURE_SHELL:           CylindricalShellTemplate(),  # 复用圆柱壳模板
    GeometryType.LAMINATED_BEAM:           LaminatedBeamTemplate(),
    GeometryType.STRINGER_STIFFENED_PANEL: StringerStiffenedPanelTemplate(),
    GeometryType.SANDWICH_PLATE:           SandwichPlateTemplate(),
    GeometryType.BOLTED_LAP_JOINT:         BoltedLapJointTemplate(),
}


class CADBuilder:
    """根据 CaseSpec 的 GeometryType 分派到对应 CAD 模板，构建参数化几何。

    使用方式：
        builder = CADBuilder()
        cad_result = builder.build(spec, output_dir=Path("runs/case_001"))
        # cad_result.step_file  → runs/case_001/model.step
        # cad_result.geometry_meta → 几何元信息（包围盒等）
    """

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        """查找对应模板并生成 STEP + geometry_meta.json。

        流程：
            1. 从 _TEMPLATE_REGISTRY 查找对应 GeometryType 的模板
            2. 调用模板的 build(spec, output_dir)
            3. 将 GeometryMeta 序列化为 geometry_meta.json

        Args:
            spec:       已验证的 CaseSpec
            output_dir: 运行目录（写入 model.step 和 geometry_meta.json）

        Returns:
            CADResult（含 step_file 路径和 GeometryMeta 对象）

        Raises:
            KeyError: 若 GeometryType 没有对应的 CAD 模板（需先在注册表中注册）
        """
        geo_type = spec.geometry.geometry_type
        template = _TEMPLATE_REGISTRY.get(geo_type)
        if template is None:
            raise KeyError(
                f"No CAD template registered for geometry type '{geo_type}'. "
                f"Available: {list(_TEMPLATE_REGISTRY.keys())}"
            )

        logger.info(f"CADBuilder → template: {template.__class__.__name__}")
        result = template.build(spec, output_dir)

        # 将 GeometryMeta 序列化保存（MeshBuilder 读取此文件获取包围盒等信息）
        meta_path = output_dir / "geometry_meta.json"
        meta_path.write_text(result.geometry_meta.to_json(), encoding="utf-8")
        logger.info(f"geometry_meta.json saved → {meta_path}")

        return result

    @staticmethod
    def list_supported_geometry_types() -> list[str]:
        """返回当前注册表中支持的所有几何类型字符串列表（供调试使用）。"""
        return [gt.value for gt in _TEMPLATE_REGISTRY]
