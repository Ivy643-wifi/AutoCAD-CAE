"""Template Registry — 管理 CaseTemplate 并执行模板匹配（G-04：模板优先）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from autocae.schemas.case_spec import AnalysisType, CaseSpec, GeometryType


@dataclass
class CaseTemplate:
    """已注册的算例模板描述符。"""
    template_id: str
    template_name: str
    topology: str
    geometry_type: GeometryType
    analysis_type: AnalysisType
    required_features: list[str] = field(default_factory=list)
    forbidden_features: list[str] = field(default_factory=list)
    default_geometry: dict[str, float] = field(default_factory=dict)
    default_layup: list[float] = field(default_factory=list)
    default_outputs: list[str] = field(default_factory=list)
    version: str = "v1"
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches(self, spec: CaseSpec) -> bool:
        """判断此模板是否与给定的 CaseSpec 兼容。"""
        if self.geometry_type != spec.geometry.geometry_type:
            return False
        if self.analysis_type != spec.analysis_type:
            return False
        enabled_features = {f.name.value for f in spec.features if f.enabled}
        for req in self.required_features:
            if req not in enabled_features:
                return False
        for forb in self.forbidden_features:
            if forb in enabled_features:
                return False
        return True


class TemplateRegistry:
    """管理已知 CaseTemplate 集合并执行匹配。

    Phase 1 内置了 7 个结构族的 15 个模板，在构造时自动注册。
    """

    def __init__(self) -> None:
        self._templates: dict[str, CaseTemplate] = {}
        self._register_phase1_templates()

    def register(self, template: CaseTemplate) -> None:
        self._templates[template.template_id] = template
        logger.debug(f"Template registered: {template.template_id}")

    def match(self, spec: CaseSpec) -> CaseTemplate | None:
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
        return self._templates.get(template_id)

    def list_templates(self) -> list[str]:
        return list(self._templates.keys())

    def load_from_dir(self, directory: Path) -> int:
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

    def _register_phase1_templates(self) -> None:
        """注册 Phase 1 所有内置模板（7 个结构族 × 2~4 个工况 = 15 个模板）。"""
        templates = [
            # ===================== FLAT PLATE =====================
            CaseTemplate(
                template_id="flat_plate_tension_v1",
                template_name="Flat Plate — Uniaxial Tension",
                topology="laminate",
                geometry_type=GeometryType.FLAT_PLATE,
                analysis_type=AnalysisType.STATIC_TENSION,
                forbidden_features=["core", "stiffener"],
                default_geometry={"length": 200.0, "width": 25.0, "thickness": 2.0},
                default_layup=[0, 45, -45, 90],
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="flat_plate_compression_v1",
                template_name="Flat Plate — Uniaxial Compression",
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
                template_name="Flat Plate — Three-Point Bending",
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
                template_name="Flat Plate — Linear Buckling",
                topology="laminate",
                geometry_type=GeometryType.FLAT_PLATE,
                analysis_type=AnalysisType.BUCKLING,
                forbidden_features=["core"],
                default_geometry={"length": 200.0, "width": 100.0, "thickness": 2.0},
                default_layup=[0, 90, 90, 0],
                default_outputs=["buckling_load_factor"],
            ),
            # ===================== OPEN HOLE PLATE =====================
            CaseTemplate(
                template_id="open_hole_plate_tension_v1",
                template_name="Open Hole Plate — Tension (OHT)",
                topology="laminate",
                geometry_type=GeometryType.OPEN_HOLE_PLATE,
                analysis_type=AnalysisType.STATIC_TENSION,
                default_geometry={"length": 300.0, "width": 36.0, "thickness": 2.0,
                                   "hole_diameter": 6.0},
                default_layup=[0, 45, -45, 90],
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="open_hole_plate_compression_v1",
                template_name="Open Hole Plate — Compression (OHC)",
                topology="laminate",
                geometry_type=GeometryType.OPEN_HOLE_PLATE,
                analysis_type=AnalysisType.STATIC_COMPRESSION,
                default_geometry={"length": 300.0, "width": 36.0, "thickness": 2.0,
                                   "hole_diameter": 6.0},
                default_layup=[0, 45, -45, 90],
                default_outputs=["max_displacement", "max_stress"],
            ),
            # ===================== CYLINDRICAL SHELL =====================
            CaseTemplate(
                template_id="cylindrical_shell_pressure_v1",
                template_name="Cylindrical Shell — Internal Pressure",
                topology="shell",
                geometry_type=GeometryType.CYLINDRICAL_SHELL,
                analysis_type=AnalysisType.PRESSURE,
                default_geometry={"length": 500.0, "width": 100.0, "thickness": 3.0,
                                   "radius": 50.0},
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="cylindrical_shell_buckling_v1",
                template_name="Cylindrical Shell — Axial Buckling",
                topology="shell",
                geometry_type=GeometryType.CYLINDRICAL_SHELL,
                analysis_type=AnalysisType.BUCKLING,
                default_geometry={"length": 500.0, "width": 100.0, "thickness": 3.0,
                                   "radius": 50.0},
                default_outputs=["buckling_load_factor"],
            ),
            # ===================== LAMINATED BEAM =====================
            CaseTemplate(
                template_id="laminated_beam_bending_v1",
                template_name="Laminated Beam — Cantilever Bending",
                topology="beam",
                geometry_type=GeometryType.LAMINATED_BEAM,
                analysis_type=AnalysisType.BENDING,
                default_geometry={"length": 500.0, "width": 25.0, "thickness": 10.0},
                default_layup=[0, 90, 90, 0],
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="laminated_beam_torsion_v1",
                template_name="Laminated Beam — Torsion",
                topology="beam",
                geometry_type=GeometryType.LAMINATED_BEAM,
                analysis_type=AnalysisType.TORSION,
                default_geometry={"length": 500.0, "width": 25.0, "thickness": 25.0},
                default_layup=[45, -45, -45, 45],
                default_outputs=["max_displacement", "max_stress"],
            ),
            # ===================== STRINGER STIFFENED PANEL =====================
            CaseTemplate(
                template_id="stringer_stiffened_panel_buckling_v1",
                template_name="Stringer Stiffened Panel — Buckling",
                topology="panel",
                geometry_type=GeometryType.STRINGER_STIFFENED_PANEL,
                analysis_type=AnalysisType.BUCKLING,
                default_geometry={"length": 600.0, "width": 300.0, "thickness": 2.5,
                                   "n_stringers": 3, "stringer_height": 20.0},
                default_outputs=["buckling_load_factor"],
            ),
            CaseTemplate(
                template_id="stringer_stiffened_panel_tension_v1",
                template_name="Stringer Stiffened Panel — Tension",
                topology="panel",
                geometry_type=GeometryType.STRINGER_STIFFENED_PANEL,
                analysis_type=AnalysisType.STATIC_TENSION,
                default_geometry={"length": 600.0, "width": 300.0, "thickness": 2.5,
                                   "n_stringers": 3},
                default_outputs=["max_displacement", "max_stress"],
            ),
            # ===================== SANDWICH PLATE =====================
            CaseTemplate(
                template_id="sandwich_plate_bending_v1",
                template_name="Sandwich Plate — Four-Point Bending",
                topology="sandwich",
                geometry_type=GeometryType.SANDWICH_PLATE,
                analysis_type=AnalysisType.BENDING,
                default_geometry={"length": 400.0, "width": 100.0, "thickness": 2.0,
                                   "core_thickness": 20.0},
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="sandwich_plate_shear_v1",
                template_name="Sandwich Plate — Short Beam Shear",
                topology="sandwich",
                geometry_type=GeometryType.SANDWICH_PLATE,
                analysis_type=AnalysisType.SHEAR,
                default_geometry={"length": 100.0, "width": 50.0, "thickness": 2.0,
                                   "core_thickness": 20.0},
                default_outputs=["max_displacement", "max_stress"],
            ),
            # ===================== BOLTED LAP JOINT =====================
            CaseTemplate(
                template_id="bolted_lap_joint_tension_v1",
                template_name="Bolted Lap Joint — Single-Shear Tension",
                topology="joint",
                geometry_type=GeometryType.BOLTED_LAP_JOINT,
                analysis_type=AnalysisType.STATIC_TENSION,
                default_geometry={"length": 200.0, "width": 40.0, "thickness": 4.0,
                                   "n_bolts": 2, "bolt_diameter": 6.35},
                default_outputs=["max_displacement", "max_stress"],
            ),
            CaseTemplate(
                template_id="bolted_lap_joint_shear_v1",
                template_name="Bolted Lap Joint — Shear Bearing",
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
