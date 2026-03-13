"""Template Registry — 管理 CaseTemplate 并执行模板匹配。

设计原则：模板优先（G-04）。
当 CaseSpec 到来时，Registry 先尝试找到匹配的模板：
    有匹配 → 用模板实例化 AnalysisModel（快速路径，结果更可靠）
    无匹配 → 从零构建 AnalysisModel（慢速路径，用于新结构形式）

Phase 1 预置了 7 个结构族 × 15 个工况的内置模板，全部硬编码在
_register_phase1_templates() 方法中。
也可以通过 load_from_dir() 从 JSON 文件目录动态加载额外模板。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from autocae.schemas.case_spec import AnalysisType, CaseSpec, GeometryType


@dataclass
class CaseTemplate:
    """已注册的算例模板描述符。

    一个 CaseTemplate 表示一类已知的、可复用的分析配置。
    当 CaseSpec 与某模板匹配时，TemplateInstantiator 会用模板的默认值
    快速填充 AnalysisModel，而不需要从零推导所有有限元参数。

    匹配规则（matches() 方法）：
        1. geometry_type 必须完全一致
        2. analysis_type 必须完全一致
        3. CaseSpec 中启用的特征不得在 forbidden_features 中出现
        4. required_features 中的特征必须全部在 CaseSpec 中启用
    """
    template_id: str          # 唯一标识（如 'flat_plate_tension_v1'）
    template_name: str        # 人类可读名称
    topology: str             # 对应的 Topology 值（字符串）
    geometry_type: GeometryType
    analysis_type: AnalysisType
    required_features: list[str] = field(default_factory=list)   # 必须存在的特征
    forbidden_features: list[str] = field(default_factory=list)  # 不允许的特征
    default_geometry: dict[str, float] = field(default_factory=dict)  # 默认几何参数
    default_layup: list[float] = field(default_factory=list)     # 默认铺层角度序列
    default_outputs: list[str] = field(default_factory=list)     # 默认输出指标
    version: str = "v1"
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches(self, spec: CaseSpec) -> bool:
        """判断此模板是否与给定的 CaseSpec 兼容（可用于实例化）。"""
        # 几何族和分析类型必须完全匹配
        if self.geometry_type != spec.geometry.geometry_type:
            return False
        if self.analysis_type != spec.analysis_type:
            return False
        # 检查特征约束
        enabled_features = {f.name.value for f in spec.features if f.enabled}
        for req in self.required_features:
            if req not in enabled_features:
                return False  # 缺少必要特征
        for forb in self.forbidden_features:
            if forb in enabled_features:
                return False  # 启用了禁止的特征
        return True


class TemplateRegistry:
    """管理已知 CaseTemplate 集合并执行匹配。

    Phase 1 内置了 7 个结构族的 15 个模板，在构造时自动注册。
    可通过 load_from_dir() 从外部 JSON 文件加载更多模板。

    主要 API：
        match(spec)         → 返回最佳匹配模板（或 None）
        get(template_id)    → 按 ID 获取模板
        list_templates()    → 列出所有已注册模板的 ID
        register(template)  → 注册一个新模板
    """

    def __init__(self) -> None:
        self._templates: dict[str, CaseTemplate] = {}
        self._register_phase1_templates()  # 内置 Phase 1 全部模板

    # ------------------------------------------------------------------
    # Public API  公开接口
    # ------------------------------------------------------------------

    def register(self, template: CaseTemplate) -> None:
        """注册一个模板（已存在同 ID 则覆盖）。"""
        self._templates[template.template_id] = template
        logger.debug(f"Template registered: {template.template_id}")

    def match(self, spec: CaseSpec) -> CaseTemplate | None:
        """为 CaseSpec 找到最佳匹配模板。

        遍历所有已注册模板，调用每个模板的 matches() 方法，
        返回第一个匹配的模板（Phase 1 中每个组合只有一个模板）。

        Returns:
            最佳匹配的 CaseTemplate；若无匹配则返回 None（触发从零构建路径）。
        """
        candidates = [t for t in self._templates.values() if t.matches(spec)]
        if not candidates:
            logger.info(
                f"No template matched for "
                f"{spec.geometry.geometry_type.value}/{spec.analysis_type.value}"
            )
            return None
        best = candidates[0]
        logger.info(
            f"Template matched: {best.template_id} "
            f"({best.geometry_type.value}/{best.analysis_type.value})"
        )
        return best

    def get(self, template_id: str) -> CaseTemplate | None:
        """按模板 ID 查询模板（未找到返回 None）。"""
        return self._templates.get(template_id)

    def list_templates(self) -> list[str]:
        """返回所有已注册模板的 ID 列表。"""
        return list(self._templates.keys())

    def load_from_dir(self, directory: Path) -> int:
        """从目录中加载所有 .json 模板文件。

        JSON 文件需符合 CaseTemplate dataclass 的字段格式。

        Returns:
            成功加载的模板数量
        """
        count = 0
        for p in directory.glob("*.json"):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                tmpl = CaseTemplate(**raw)
                self.register(tmpl)
                count += 1
            except Exception as exc:
                logger.warning(f"Could not load template {p.name}: {exc}")
        return count

    # ------------------------------------------------------------------
    # Phase 1 内置模板（7 个结构族 × 2~4 个工况 = 15 个模板）
    # ------------------------------------------------------------------

    def _register_phase1_templates(self) -> None:
        """注册 Phase 1 所有内置模板。

        模板 ID 命名规则：<geometry_type>_<analysis_type>_v<version>
        每个模板包含：
            - geometry_type + analysis_type（匹配键）
            - forbidden_features（不允许的特征）
            - default_geometry（默认几何尺寸，参考标准试验件规格）
            - default_layup（推荐铺层序列）
            - default_outputs（期望输出的结果指标）
        """

        templates = [
            # ===================== FLAT PLATE 矩形平板 =====================
            CaseTemplate(
                template_id="flat_plate_tension_v1",
                template_name="Flat Plate — Uniaxial Tension",  # 单轴拉伸
                topology="laminate",
                geometry_type=GeometryType.FLAT_PLATE,
                analysis_type=AnalysisType.STATIC_TENSION,
                forbidden_features=["core", "stiffener"],
                # 200×25×2 mm：ASTM D3039 标准拉伸试验件尺寸
                default_geometry={"length": 200.0, "width": 25.0, "thickness": 2.0},
                default_layup=[0, 45, -45, 90],             # 准各向同性铺层
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="flat_plate_compression_v1",
                template_name="Flat Plate — Uniaxial Compression",  # 单轴压缩
                topology="laminate",
                geometry_type=GeometryType.FLAT_PLATE,
                analysis_type=AnalysisType.STATIC_COMPRESSION,
                forbidden_features=["core", "stiffener"],
                default_geometry={"length": 200.0, "width": 25.0, "thickness": 2.0},
                default_layup=[0, 90, 90, 0],
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="flat_plate_bending_v1",
                template_name="Flat Plate — Three-Point Bending",  # 三点弯曲
                topology="laminate",
                geometry_type=GeometryType.FLAT_PLATE,
                analysis_type=AnalysisType.BENDING,
                forbidden_features=["core"],
                default_geometry={"length": 200.0, "width": 25.0, "thickness": 2.0},
                default_layup=[0, 45, -45, 90],
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="flat_plate_buckling_v1",
                template_name="Flat Plate — Linear Buckling",  # 线性屈曲
                topology="laminate",
                geometry_type=GeometryType.FLAT_PLATE,
                analysis_type=AnalysisType.BUCKLING,
                forbidden_features=["core"],
                # 屈曲试验件通常宽一些（宽高比接近 1）
                default_geometry={"length": 200.0, "width": 100.0, "thickness": 2.0},
                default_layup=[0, 90, 90, 0],
                default_outputs=["buckling_load_factor"],
            ),
            # ===================== OPEN HOLE PLATE 开孔平板 =====================
            CaseTemplate(
                template_id="open_hole_plate_tension_v1",
                template_name="Open Hole Plate — Tension (OHT)",  # 开孔拉伸
                topology="laminate",
                geometry_type=GeometryType.OPEN_HOLE_PLATE,
                analysis_type=AnalysisType.STATIC_TENSION,
                # 孔径 6mm，宽 36mm → 孔径/宽 = 1/6，符合 ASTM D5766
                default_geometry={"length": 300.0, "width": 36.0, "thickness": 2.0,
                                   "hole_diameter": 6.0},
                default_layup=[0, 45, -45, 90],
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="open_hole_plate_compression_v1",
                template_name="Open Hole Plate — Compression (OHC)",  # 开孔压缩
                topology="laminate",
                geometry_type=GeometryType.OPEN_HOLE_PLATE,
                analysis_type=AnalysisType.STATIC_COMPRESSION,
                default_geometry={"length": 300.0, "width": 36.0, "thickness": 2.0,
                                   "hole_diameter": 6.0},
                default_layup=[0, 45, -45, 90],
                default_outputs=["max_displacement", "max_stress"],
            ),
            # ===================== CYLINDRICAL SHELL 圆柱壳 =====================
            CaseTemplate(
                template_id="cylindrical_shell_pressure_v1",
                template_name="Cylindrical Shell — Internal Pressure",  # 内压
                topology="shell",
                geometry_type=GeometryType.CYLINDRICAL_SHELL,
                analysis_type=AnalysisType.PRESSURE,
                # radius=50mm（圆柱半径），length=500mm
                default_geometry={"length": 500.0, "width": 100.0, "thickness": 3.0,
                                   "radius": 50.0},
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="cylindrical_shell_buckling_v1",
                template_name="Cylindrical Shell — Axial Buckling",  # 轴向屈曲
                topology="shell",
                geometry_type=GeometryType.CYLINDRICAL_SHELL,
                analysis_type=AnalysisType.BUCKLING,
                default_geometry={"length": 500.0, "width": 100.0, "thickness": 3.0,
                                   "radius": 50.0},
                default_outputs=["buckling_load_factor"],
            ),
            # ===================== LAMINATED BEAM 层合梁 =====================
            CaseTemplate(
                template_id="laminated_beam_bending_v1",
                template_name="Laminated Beam — Cantilever Bending",  # 悬臂弯曲
                topology="beam",
                geometry_type=GeometryType.LAMINATED_BEAM,
                analysis_type=AnalysisType.BENDING,
                default_geometry={"length": 500.0, "width": 25.0, "thickness": 10.0},
                default_layup=[0, 90, 90, 0],
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="laminated_beam_torsion_v1",
                template_name="Laminated Beam — Torsion",  # 扭转
                topology="beam",
                geometry_type=GeometryType.LAMINATED_BEAM,
                analysis_type=AnalysisType.TORSION,
                # ±45° 铺层对扭转刚度最有利
                default_geometry={"length": 500.0, "width": 25.0, "thickness": 25.0},
                default_layup=[45, -45, -45, 45],
                default_outputs=["max_displacement", "max_stress"],
            ),
            # ===================== STRINGER STIFFENED PANEL 长桁加筋壁板 =====================
            CaseTemplate(
                template_id="stringer_stiffened_panel_buckling_v1",
                template_name="Stringer Stiffened Panel — Buckling",  # 屈曲
                topology="panel",
                geometry_type=GeometryType.STRINGER_STIFFENED_PANEL,
                analysis_type=AnalysisType.BUCKLING,
                # n_stringers=3（3 根长桁），stringer_height=20mm
                default_geometry={"length": 600.0, "width": 300.0, "thickness": 2.5,
                                   "n_stringers": 3, "stringer_height": 20.0},
                default_outputs=["buckling_load_factor"],
            ),
            CaseTemplate(
                template_id="stringer_stiffened_panel_tension_v1",
                template_name="Stringer Stiffened Panel — Tension",  # 拉伸
                topology="panel",
                geometry_type=GeometryType.STRINGER_STIFFENED_PANEL,
                analysis_type=AnalysisType.STATIC_TENSION,
                default_geometry={"length": 600.0, "width": 300.0, "thickness": 2.5,
                                   "n_stringers": 3},
                default_outputs=["max_displacement", "max_stress"],
            ),
            # ===================== SANDWICH PLATE 夹芯板 =====================
            CaseTemplate(
                template_id="sandwich_plate_bending_v1",
                template_name="Sandwich Plate — Four-Point Bending",  # 四点弯曲
                topology="sandwich",
                geometry_type=GeometryType.SANDWICH_PLATE,
                analysis_type=AnalysisType.BENDING,
                # core_thickness=20mm（蜂窝芯厚度）
                default_geometry={"length": 400.0, "width": 100.0, "thickness": 2.0,
                                   "core_thickness": 20.0},
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="sandwich_plate_shear_v1",
                template_name="Sandwich Plate — Short Beam Shear",  # 短梁剪切
                topology="sandwich",
                geometry_type=GeometryType.SANDWICH_PLATE,
                analysis_type=AnalysisType.SHEAR,
                default_geometry={"length": 100.0, "width": 50.0, "thickness": 2.0,
                                   "core_thickness": 20.0},
                default_outputs=["max_displacement", "max_stress"],
            ),
            # ===================== BOLTED LAP JOINT 螺接搭接接头 =====================
            CaseTemplate(
                template_id="bolted_lap_joint_tension_v1",
                template_name="Bolted Lap Joint — Single-Shear Tension",  # 单剪拉伸
                topology="joint",
                geometry_type=GeometryType.BOLTED_LAP_JOINT,
                analysis_type=AnalysisType.STATIC_TENSION,
                # n_bolts=2（2 颗螺栓），bolt_diameter=6.35mm（1/4 英寸）
                default_geometry={"length": 200.0, "width": 40.0, "thickness": 4.0,
                                   "n_bolts": 2, "bolt_diameter": 6.35},
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="bolted_lap_joint_shear_v1",
                template_name="Bolted Lap Joint — Shear Bearing",  # 剪切承压
                topology="joint",
                geometry_type=GeometryType.BOLTED_LAP_JOINT,
                analysis_type=AnalysisType.SHEAR,
                default_geometry={"length": 200.0, "width": 40.0, "thickness": 4.0,
                                   "n_bolts": 2, "bolt_diameter": 6.35},
                default_outputs=["max_displacement", "max_stress"],
            ),
        ]

        for t in templates:
            self.register(t)

        logger.info(f"Phase 1 template library: {len(templates)} templates registered.")
