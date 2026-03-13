"""CaseSpec validator — 四层诊断能力中的第一层（Layer A：输入验证）。

校验内容：
  - Schema 合规性（由 Pydantic 在模型构造时自动完成）
  - 跨字段业务规则（Topology ↔ GeometryType 兼容性）
  - 特征与结构族的兼容性（例如平板不能有加筋）
  - 分析类型与结构族的兼容性（例如压力分析不适用于平板）
  - 单位合理性检查（极端长宽比警告、厚度异常警告）

注意：此模块只做 Layer A（输入层）。
      Layer B/C/D 由 diagnostics/validator.py 中的 DiagnosticsValidator 实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from autocae.schemas.case_spec import (
    AnalysisType,
    CaseSpec,
    FeatureName,
    GeometryType,
    Topology,
)

# ---------------------------------------------------------------------------
# Compatibility tables  兼容性表（来自 CASESPEC_DESIGN_summary 规范）
# ---------------------------------------------------------------------------

#: Topology → 允许的 GeometryType 集合
#: 这是三级层级结构的第一个映射关系
_TOPOLOGY_GEOMETRY: dict[Topology, set[GeometryType]] = {
    Topology.LAMINATE: {
        GeometryType.FLAT_PLATE,        # 矩形平板
        GeometryType.OPEN_HOLE_PLATE,   # 开孔平板
        GeometryType.NOTCHED_PLATE,     # 缺口平板
    },
    Topology.SHELL: {
        GeometryType.CYLINDRICAL_SHELL,  # 圆柱壳
        GeometryType.PRESSURE_SHELL,     # 压力壳
    },
    Topology.BEAM: {
        GeometryType.LAMINATED_BEAM,     # 层合梁
    },
    Topology.PANEL: {
        GeometryType.STRINGER_STIFFENED_PANEL,  # 长桁加筋壁板
    },
    Topology.SANDWICH: {
        GeometryType.SANDWICH_PLATE,     # 夹芯板
    },
    Topology.JOINT: {
        GeometryType.BOLTED_LAP_JOINT,   # 螺接搭接接头
    },
}

#: GeometryType → 允许的 AnalysisType 集合
#: 这是三级层级结构的第二个映射关系
_GEOMETRY_ANALYSIS: dict[GeometryType, set[AnalysisType]] = {
    GeometryType.FLAT_PLATE: {
        AnalysisType.STATIC_TENSION,
        AnalysisType.STATIC_COMPRESSION,
        AnalysisType.BENDING,
        AnalysisType.BUCKLING,
        AnalysisType.MODAL,
    },
    GeometryType.OPEN_HOLE_PLATE: {
        AnalysisType.STATIC_TENSION,
        AnalysisType.STATIC_COMPRESSION,
    },
    GeometryType.NOTCHED_PLATE: {
        AnalysisType.STATIC_TENSION,
        AnalysisType.STATIC_COMPRESSION,
    },
    GeometryType.CYLINDRICAL_SHELL: {
        AnalysisType.PRESSURE,
        AnalysisType.BUCKLING,
        AnalysisType.MODAL,
    },
    GeometryType.PRESSURE_SHELL: {
        AnalysisType.PRESSURE,
        AnalysisType.BUCKLING,
    },
    GeometryType.LAMINATED_BEAM: {
        AnalysisType.BENDING,
        AnalysisType.TORSION,
        AnalysisType.MODAL,
    },
    GeometryType.STRINGER_STIFFENED_PANEL: {
        AnalysisType.BUCKLING,
        AnalysisType.STATIC_TENSION,
        AnalysisType.STATIC_COMPRESSION,
    },
    GeometryType.SANDWICH_PLATE: {
        AnalysisType.BENDING,
        AnalysisType.SHEAR,
        AnalysisType.BUCKLING,
    },
    GeometryType.BOLTED_LAP_JOINT: {
        AnalysisType.STATIC_TENSION,
        AnalysisType.SHEAR,
    },
}

#: GeometryType → 禁止启用的 Feature（特征与几何族不兼容）
_FORBIDDEN_FEATURES: dict[GeometryType, set[FeatureName]] = {
    GeometryType.FLAT_PLATE: {FeatureName.CORE, FeatureName.STIFFENER},
    # 平板不能有芯材（那是夹芯板）或加筋（那是加筋板）
    GeometryType.SANDWICH_PLATE: {FeatureName.STIFFENER},
    GeometryType.LAMINATED_BEAM: {FeatureName.CORE},
}


@dataclass
class ValidationResult:
    """验证结果容器：汇总所有错误和警告。

    passed=False 说明有致命错误，流水线不会继续。
    passed=True 但有 warnings 说明存在潜在问题，流水线会继续并输出提示。
    """
    passed: bool = True
    errors: list[str] = field(default_factory=list)   # 致命错误（阻断流水线）
    warnings: list[str] = field(default_factory=list) # 警告（不阻断，但提示用户）

    def add_error(self, msg: str) -> None:
        self.passed = False
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


class CaseSpecValidator:
    """对 CaseSpec 进行超出 Pydantic Schema 范围的业务规则校验。

    校验顺序：
        1. Topology ↔ GeometryType 兼容性
        2. AnalysisType ↔ GeometryType 兼容性
        3. Feature 与 GeometryType 兼容性
        4. 材料 ID 引用完整性（铺层中引用的材料必须存在）
        5. 几何尺寸合理性（长宽比、厚度比）
    """

    def validate(self, spec: CaseSpec) -> ValidationResult:
        """对给定的 CaseSpec 执行全部业务规则校验。

        Args:
            spec: 已通过 Pydantic Schema 校验的 CaseSpec 对象

        Returns:
            ValidationResult（包含是否通过、错误列表、警告列表）
        """
        result = ValidationResult()
        geo_type = spec.geometry.geometry_type

        self._check_topology_geometry(spec, result)
        self._check_analysis_type(spec, result, geo_type)
        self._check_features(spec, result, geo_type)
        self._check_material_completeness(spec, result)
        self._check_geometry_dimensions(spec, result)

        if result.passed:
            logger.info(f"CaseSpec validation PASSED for '{spec.metadata.case_name}'")
        else:
            for err in result.errors:
                logger.error(f"Validation error: {err}")
        for warn in result.warnings:
            logger.warning(f"Validation warning: {warn}")

        return result

    # ------------------------------------------------------------------
    # 私有校验方法
    # ------------------------------------------------------------------

    def _check_topology_geometry(self, spec: CaseSpec, result: ValidationResult) -> None:
        """校验 Topology ↔ GeometryType 兼容性。
        例如：SHELL 拓扑只允许 CYLINDRICAL_SHELL 或 PRESSURE_SHELL，
              用 FLAT_PLATE 就会报错。
        """
        allowed = _TOPOLOGY_GEOMETRY.get(spec.topology, set())
        if spec.geometry.geometry_type not in allowed:
            result.add_error(
                f"Geometry type '{spec.geometry.geometry_type}' is not compatible "
                f"with topology '{spec.topology}'. Allowed: {[g.value for g in allowed]}"
            )

    def _check_analysis_type(
        self, spec: CaseSpec, result: ValidationResult, geo_type: GeometryType
    ) -> None:
        """校验 AnalysisType ↔ GeometryType 兼容性。
        例如：PRESSURE 分析只适用于 CYLINDRICAL_SHELL 或 PRESSURE_SHELL，
              对 FLAT_PLATE 使用 PRESSURE 会报错。
        """
        allowed = _GEOMETRY_ANALYSIS.get(geo_type, set())
        if spec.analysis_type not in allowed:
            result.add_error(
                f"Analysis type '{spec.analysis_type}' is not supported for "
                f"geometry '{geo_type}'. Allowed: {[a.value for a in allowed]}"
            )

    def _check_features(
        self, spec: CaseSpec, result: ValidationResult, geo_type: GeometryType
    ) -> None:
        """校验特征与几何族的兼容性。
        例如：平板（FLAT_PLATE）禁止启用 STIFFENER（加筋）特征，
              因为带加筋的平板应归类为 STRINGER_STIFFENED_PANEL。
        """
        forbidden = _FORBIDDEN_FEATURES.get(geo_type, set())
        for feat in spec.features:
            if feat.enabled and feat.name in forbidden:
                result.add_error(
                    f"Feature '{feat.name}' is forbidden for geometry type '{geo_type}'."
                )

    def _check_material_completeness(self, spec: CaseSpec, result: ValidationResult) -> None:
        """校验铺层中引用的材料 ID 是否都存在于 materials 列表中。
        （跨字段引用完整性检查）
        """
        mat_ids = {m.material_id for m in spec.materials}
        for layer in spec.layup:
            if layer.material_id not in mat_ids and layer.material_id != "default":
                result.add_error(
                    f"Layup references unknown material_id '{layer.material_id}'. "
                    f"Available: {list(mat_ids)}"
                )

    def _check_geometry_dimensions(self, spec: CaseSpec, result: ValidationResult) -> None:
        """对几何尺寸进行合理性检查（警告级别，不阻断流水线）。

        检查项：
            1. 长宽比 > 20：可能导致网格质量差
            2. 厚度 > min(长, 宽) × 0.5：违反壳/板理论假设
        """
        geo = spec.geometry
        aspect = max(geo.length, geo.width) / min(geo.length, geo.width)
        if aspect > 20:
            result.add_warning(
                f"High aspect ratio ({aspect:.1f}). Mesh quality may suffer."
            )
        min_dim = min(geo.length, geo.width)
        if geo.thickness > min_dim * 0.5:
            result.add_warning(
                f"Thickness ({geo.thickness} mm) is large relative to lateral "
                f"dimensions ({min_dim} mm). Shell assumption may not hold."
            )
