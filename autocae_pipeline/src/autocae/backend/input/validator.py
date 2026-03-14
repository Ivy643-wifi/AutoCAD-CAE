"""Input validator — Layer A (CaseSpec business rules) + Layers B/C/D (diagnostics).

Layer A — CaseSpec 输入验证（CaseSpecValidator）:
  - Schema 合规性（Pydantic 自动完成）
  - Topology ↔ GeometryType 兼容性
  - AnalysisType ↔ GeometryType 兼容性
  - Feature 与结构族兼容性
  - 材料引用完整性
  - 几何尺寸合理性检查

Layer B — 接口验证（DiagnosticsValidator）:
  - STEP / mesh 文件存在性和大小检查

Layer C — 运行时诊断（DiagnosticsValidator）:
  - 求解器日志解析（收敛性、致命错误检测）

Layer D — 修复建议（通过 DiagnosticCheck.suggestion 传递）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from autocae.schemas.case_spec import (
    AnalysisType,
    CaseSpec,
    FeatureName,
    GeometryType,
    Topology,
)
from autocae.schemas.postprocess import Diagnostics, DiagnosticCheck


# ---------------------------------------------------------------------------
# 兼容性查表：定义各层级之间的合法组合关系
# ---------------------------------------------------------------------------

# 拓扑类型 → 允许的几何类型集合（Topology → GeometryType）
_TOPOLOGY_GEOMETRY: dict[Topology, set[GeometryType]] = {
    Topology.LAMINATE: {
        GeometryType.FLAT_PLATE,
        GeometryType.OPEN_HOLE_PLATE,
        GeometryType.NOTCHED_PLATE,
    },
    Topology.SHELL: {
        GeometryType.CYLINDRICAL_SHELL,
        GeometryType.PRESSURE_SHELL,
    },
    Topology.BEAM: {
        GeometryType.LAMINATED_BEAM,
    },
    Topology.PANEL: {
        GeometryType.STRINGER_STIFFENED_PANEL,
    },
    Topology.SANDWICH: {
        GeometryType.SANDWICH_PLATE,
    },
    Topology.JOINT: {
        GeometryType.BOLTED_LAP_JOINT,
    },
}

# 几何类型 → 允许的分析类型集合（GeometryType → AnalysisType）
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

# 几何类型 → 禁止使用的结构特征集合（避免物理上不合理的组合）
_FORBIDDEN_FEATURES: dict[GeometryType, set[FeatureName]] = {
    GeometryType.FLAT_PLATE: {FeatureName.CORE, FeatureName.STIFFENER},  # 平板不能有芯材或加筋
    GeometryType.SANDWICH_PLATE: {FeatureName.STIFFENER},                # 夹芯板不能有外部加筋
    GeometryType.LAMINATED_BEAM: {FeatureName.CORE},                     # 层合梁不能有芯材
}


# ---------------------------------------------------------------------------
# Layer A — CaseSpec 业务规则校验
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """验证结果容器：汇总所有错误和警告。"""
    passed: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        """记录错误（同时将 passed 置为 False）。"""
        self.passed = False
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        """记录警告（不影响 passed 状态）。"""
        self.warnings.append(msg)


class CaseSpecValidator:
    """Layer A — 对 CaseSpec 进行业务规则校验。"""

    def validate(self, spec: CaseSpec) -> ValidationResult:
        """运行所有 Layer A 校验规则，返回汇总结果。"""
        result = ValidationResult()
        geo_type = spec.geometry.geometry_type

        # 依次执行五项校验（顺序无关）
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

    def _check_topology_geometry(self, spec: CaseSpec, result: ValidationResult) -> None:
        """校验 Topology ↔ GeometryType 兼容性（如 laminate 不能搭配 cylindrical_shell）。"""
        allowed = _TOPOLOGY_GEOMETRY.get(spec.topology, set())
        if spec.geometry.geometry_type not in allowed:
            result.add_error(
                f"Geometry type '{spec.geometry.geometry_type}' is not compatible "
                f"with topology '{spec.topology}'. Allowed: {[g.value for g in allowed]}"
            )

    def _check_analysis_type(
        self, spec: CaseSpec, result: ValidationResult, geo_type: GeometryType
    ) -> None:
        """校验 AnalysisType ↔ GeometryType 兼容性（如平板不支持 pressure 分析）。"""
        allowed = _GEOMETRY_ANALYSIS.get(geo_type, set())
        if spec.analysis_type not in allowed:
            result.add_error(
                f"Analysis type '{spec.analysis_type}' is not supported for "
                f"geometry '{geo_type}'. Allowed: {[a.value for a in allowed]}"
            )

    def _check_features(
        self, spec: CaseSpec, result: ValidationResult, geo_type: GeometryType
    ) -> None:
        """校验特征与结构族兼容性（禁止使用不合理特征，如平板上加芯材）。"""
        forbidden = _FORBIDDEN_FEATURES.get(geo_type, set())
        for feat in spec.features:
            if feat.enabled and feat.name in forbidden:
                result.add_error(
                    f"Feature '{feat.name}' is forbidden for geometry type '{geo_type}'."
                )

    def _check_material_completeness(self, spec: CaseSpec, result: ValidationResult) -> None:
        """校验铺层引用的材料 ID 均存在于 materials 列表中。"""
        mat_ids = {m.material_id for m in spec.materials}
        for layer in spec.layup:
            if layer.material_id not in mat_ids and layer.material_id != "default":
                result.add_error(
                    f"Layup references unknown material_id '{layer.material_id}'. "
                    f"Available: {list(mat_ids)}"
                )

    def _check_geometry_dimensions(self, spec: CaseSpec, result: ValidationResult) -> None:
        """校验几何尺寸合理性，发出高长宽比和厚度偏大的警告。"""
        geo = spec.geometry
        # 长宽比超过 20 时，网格质量可能严重退化
        aspect = max(geo.length, geo.width) / min(geo.length, geo.width)
        if aspect > 20:
            result.add_warning(
                f"High aspect ratio ({aspect:.1f}). Mesh quality may suffer."
            )
        # 厚度超过最小横向尺寸的 50% 时，薄壳假设不再成立
        min_dim = min(geo.length, geo.width)
        if geo.thickness > min_dim * 0.5:
            result.add_warning(
                f"Thickness ({geo.thickness} mm) is large relative to lateral "
                f"dimensions ({min_dim} mm). Shell assumption may not hold."
            )


# ---------------------------------------------------------------------------
# Layers B / C / D — 接口校验 + 运行时诊断
# ---------------------------------------------------------------------------

class DiagnosticsValidator:
    """Layers B–D 诊断：文件接口校验 + 求解器日志解析。"""

    # ------------------------------------------------------------------
    # Layer B — 接口校验
    # ------------------------------------------------------------------

    def check_step_file(self, step_path: str | Path) -> DiagnosticCheck:
        """校验 STEP 文件是否存在且大小合理（Layer B）。"""
        p = Path(step_path)
        if not p.exists():
            return DiagnosticCheck(
                layer="interface",
                check_name="step_file_exists",
                passed=False,
                message=f"STEP file not found: {p}",
                suggestion="Verify CADBuilder ran successfully and model.step was exported.",
            )
        if p.stat().st_size < 100:
            # 文件过小说明 CadQuery 导出可能失败（正常 STEP 文件至少数 KB）
            return DiagnosticCheck(
                layer="interface",
                check_name="step_file_size",
                passed=False,
                message=f"STEP file is suspiciously small ({p.stat().st_size} bytes).",
                suggestion="Re-run CADBuilder and check for CadQuery export errors.",
            )
        return DiagnosticCheck(
            layer="interface", check_name="step_file_exists", passed=True
        )

    def check_mesh_file(self, mesh_path: str | Path) -> DiagnosticCheck:
        """校验 mesh.inp 网格文件是否存在（Layer B）。"""
        p = Path(mesh_path)
        if not p.exists():
            return DiagnosticCheck(
                layer="interface",
                check_name="mesh_file_exists",
                passed=False,
                message=f"Mesh file not found: {p}",
                suggestion="Verify MeshBuilder ran successfully.",
            )
        return DiagnosticCheck(
            layer="interface", check_name="mesh_file_exists", passed=True
        )

    def check_physical_groups(
        self, groups_path: str | Path, required_groups: list[str]
    ) -> list[DiagnosticCheck]:
        """校验 mesh_groups.json 中是否包含所有必要的 Physical Group（Layer B）。

        每个 required_groups 中的名称对应求解器集合名（如 'FIXED_END'）。
        缺失的组会导致后续 SolverAdapter 无法正确写入边界条件。
        """
        from autocae.schemas.mesh import MeshGroups

        results: list[DiagnosticCheck] = []
        try:
            mg = MeshGroups.from_json(str(groups_path))
            assigned = {g.solver_set_name for g in mg.groups}
            for name in required_groups:
                if name not in assigned:
                    results.append(DiagnosticCheck(
                        layer="interface",
                        check_name=f"physical_group_{name}",
                        passed=False,
                        message=f"Required physical group '{name}' not found in mesh.",
                        suggestion=(
                            "Check that the named face heuristics in MeshBuilder "
                            "match the CAD template's bounding box."
                        ),
                    ))
                else:
                    results.append(DiagnosticCheck(
                        layer="interface",
                        check_name=f"physical_group_{name}",
                        passed=True,
                    ))
        except Exception as exc:
            # mesh_groups.json 解析失败（格式错误或文件损坏）
            results.append(DiagnosticCheck(
                layer="interface",
                check_name="mesh_groups_parseable",
                passed=False,
                message=f"Could not load mesh_groups.json: {exc}",
            ))
        return results

    # ------------------------------------------------------------------
    # Layer C — 运行时诊断
    # ------------------------------------------------------------------

    def parse_ccx_log(self, log_path: str | Path) -> list[DiagnosticCheck]:
        """解析 CalculiX ccx_run.log，检测收敛性和致命错误（Layer C）。

        收敛判断：
            日志中含 "converged" 或 "j o b   c o m p l e t e d" → 收敛
        致命错误检测：
            日志中含 "ERROR", "FATAL", "Segmentation" → 有致命错误
        """
        results: list[DiagnosticCheck] = []
        p = Path(log_path)
        if not p.exists():
            return results  # 日志不存在时不做判断（可能是 dry_run 模式）

        # CalculiX 日志用 latin-1 编码，含非 ASCII 字符时不报错
        text = p.read_text(encoding="latin-1", errors="replace")

        # 检查收敛性（关键字大小写不敏感）
        converged = ("converged" in text.lower() or
                     "j o b   c o m p l e t e d" in text.lower())
        results.append(DiagnosticCheck(
            layer="runtime",
            check_name="ccx_convergence",
            passed=converged,
            message="Job converged." if converged else "Convergence not detected in log.",
            suggestion="" if converged else (
                "Check boundary conditions, material properties, and mesh quality. "
                "Consider enabling nlgeom or reducing load increments."
            ),
        ))

        # 检查致命错误（区分大小写，CalculiX 日志中这些词均为大写）
        fatal = any(word in text for word in ("ERROR", "FATAL", "Segmentation"))
        if fatal:
            results.append(DiagnosticCheck(
                layer="runtime",
                check_name="ccx_no_fatal_error",
                passed=False,
                message="Fatal error detected in solver log.",
                suggestion="Review ccx_run.log for the specific error message.",
            ))
        else:
            results.append(DiagnosticCheck(
                layer="runtime",
                check_name="ccx_no_fatal_error",
                passed=True,
            ))

        return results
