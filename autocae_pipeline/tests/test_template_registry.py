"""Tests for the Template Registry (template matching logic)."""

from __future__ import annotations

import pytest

from autocae.schemas.case_spec import (
    AnalysisType,
    BoundaryCondition,
    BoundaryType,
    CaseSpec,
    CaseSpecMetadata,
    Feature,
    FeatureName,
    Geometry,
    GeometryType,
    LayupLayer,
    Load,
    LoadType,
    Material,
    MeshPreferences,
    Topology,
)
from autocae.backend.templates.registry import TemplateRegistry


def make_flat_plate_tension_spec() -> CaseSpec:
    return CaseSpec(
        metadata=CaseSpecMetadata(case_name="fp_tension"),
        topology=Topology.LAMINATE,
        geometry=Geometry(
            geometry_type=GeometryType.FLAT_PLATE,
            length=200.0, width=25.0, thickness=2.0,
        ),
        layup=[LayupLayer(angle=a, thickness=0.25) for a in [0, 45, -45, 90]],
        materials=[Material(name="Carbon", E1=135000.0, E2=10000.0, G12=5200.0, nu12=0.3)],
        loads=[Load(load_type=LoadType.TENSION, magnitude=1000.0, location="LOAD_END")],
        boundary_conditions=[BoundaryCondition(bc_type=BoundaryType.FIXED, location="FIXED_END")],
        analysis_type=AnalysisType.STATIC_TENSION,
        mesh_preferences=MeshPreferences(global_size=2.0),
    )


class TestTemplateRegistry:
    def setup_method(self) -> None:
        self.registry = TemplateRegistry()

    def test_phase1_templates_registered(self) -> None:
        templates = self.registry.list_templates()
        assert len(templates) >= 15   # Phase 1: at least 15 templates

    def test_flat_plate_tension_matched(self) -> None:
        spec = make_flat_plate_tension_spec()
        tmpl = self.registry.match(spec)
        assert tmpl is not None
        assert tmpl.geometry_type == GeometryType.FLAT_PLATE
        assert tmpl.analysis_type == AnalysisType.STATIC_TENSION

    def test_no_match_for_unknown_combo(self) -> None:
        """A valid but unsupported combination should return None."""
        spec = make_flat_plate_tension_spec()
        spec.analysis_type = AnalysisType.THERMAL
        tmpl = self.registry.match(spec)
        # THERMAL not registered for flat_plate → no match
        assert tmpl is None

    def test_forbidden_feature_prevents_match(self) -> None:
        spec = make_flat_plate_tension_spec()
        # Add stiffener (forbidden for flat_plate templates)
        spec.features = [Feature(name=FeatureName.STIFFENER, enabled=True)]
        tmpl = self.registry.match(spec)
        # flat_plate_tension templates forbid stiffener → no match
        assert tmpl is None

    def test_all_phase1_families_covered(self) -> None:
        """Every Phase 1 structural family must have at least 2 templates."""
        from autocae.schemas.case_spec import GeometryType
        families = [gt for gt in GeometryType if gt != GeometryType.NOTCHED_PLATE
                    and gt != GeometryType.PRESSURE_SHELL]
        templates = [self.registry.get(tid) for tid in self.registry.list_templates()]
        for family in families:
            count = sum(1 for t in templates if t and t.geometry_type == family)
            assert count >= 2, f"Family {family.value} has only {count} template(s)"

    def test_get_template_by_id(self) -> None:
        tmpl = self.registry.get("flat_plate_tension_v1")
        assert tmpl is not None
        assert tmpl.template_id == "flat_plate_tension_v1"

    def test_cylindrical_shell_pressure_matched(self) -> None:
        spec = CaseSpec(
            metadata=CaseSpecMetadata(case_name="cs_pressure"),
            topology=Topology.SHELL,
            geometry=Geometry(
                geometry_type=GeometryType.CYLINDRICAL_SHELL,
                length=500.0, width=100.0, thickness=3.0,
                extra={"radius": 50.0},
            ),
            materials=[Material(name="Al", E=71700.0, nu=0.33)],
            loads=[Load(load_type=LoadType.PRESSURE, magnitude=1.0, location="INNER_SURFACE")],
            boundary_conditions=[BoundaryCondition(bc_type=BoundaryType.PINNED, location="END_A")],
            analysis_type=AnalysisType.PRESSURE,
            mesh_preferences=MeshPreferences(global_size=5.0),
        )
        tmpl = self.registry.match(spec)
        assert tmpl is not None
        assert tmpl.geometry_type == GeometryType.CYLINDRICAL_SHELL
