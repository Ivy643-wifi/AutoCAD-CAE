"""Tests for CaseSpec schema validation and business rule enforcement."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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
from autocae.backend.input.validator import CaseSpecValidator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def carbon_material() -> Material:
    return Material(
        material_id="carbon_ud",
        name="Carbon_UD",
        E1=135000.0, E2=10000.0, G12=5200.0, nu12=0.3,
    )


@pytest.fixture()
def aluminium_material() -> Material:
    return Material(
        material_id="al7075",
        name="Aluminium_7075",
        E=71700.0, nu=0.33,
    )


@pytest.fixture()
def standard_layup() -> list[LayupLayer]:
    angles = [0, 45, -45, 90, 90, -45, 45, 0]
    return [LayupLayer(angle=a, thickness=0.25) for a in angles]


def _make_spec(
    topology: Topology,
    geo_type: GeometryType,
    analysis_type: AnalysisType,
    materials: list[Material],
    layup: list[LayupLayer] | None = None,
    extra: dict | None = None,
) -> CaseSpec:
    return CaseSpec(
        metadata=CaseSpecMetadata(case_name="test_case"),
        topology=topology,
        geometry=Geometry(
            geometry_type=geo_type,
            length=200.0, width=25.0, thickness=2.0,
            extra=extra or {},
        ),
        layup=layup or [],
        materials=materials,
        loads=[Load(load_type=LoadType.TENSION, magnitude=1000.0, location="LOAD_END")],
        boundary_conditions=[BoundaryCondition(bc_type=BoundaryType.FIXED, location="FIXED_END")],
        analysis_type=analysis_type,
        mesh_preferences=MeshPreferences(global_size=2.0),
    )


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestMaterialSchema:
    def test_isotropic_material_valid(self) -> None:
        m = Material(name="Steel", E=210000.0, nu=0.3)
        assert m.E == 210000.0

    def test_orthotropic_material_valid(self, carbon_material: Material) -> None:
        assert carbon_material.E1 == 135000.0

    def test_material_without_properties_raises(self) -> None:
        with pytest.raises(ValidationError):
            Material(name="NoProps")

    def test_material_invalid_nu(self) -> None:
        with pytest.raises(ValidationError):
            Material(name="Bad", E=200000.0, nu=0.6)   # nu >= 0.5


class TestGeometrySchema:
    def test_negative_length_raises(self) -> None:
        with pytest.raises(ValidationError):
            Geometry(geometry_type=GeometryType.FLAT_PLATE, length=-10.0, width=25.0, thickness=2.0)

    def test_zero_thickness_raises(self) -> None:
        with pytest.raises(ValidationError):
            Geometry(geometry_type=GeometryType.FLAT_PLATE, length=200.0, width=25.0, thickness=0.0)

    def test_extra_negative_value_raises(self) -> None:
        with pytest.raises(ValidationError):
            Geometry(
                geometry_type=GeometryType.OPEN_HOLE_PLATE,
                length=200.0, width=25.0, thickness=2.0,
                extra={"hole_diameter": -5.0},
            )


class TestCaseSpecSchema:
    def test_laminate_without_layup_raises(
        self, carbon_material: Material
    ) -> None:
        with pytest.raises(ValidationError, match="LAMINATE"):
            _make_spec(
                Topology.LAMINATE,
                GeometryType.FLAT_PLATE,
                AnalysisType.STATIC_TENSION,
                [carbon_material],
                layup=[],  # empty layup for laminate → should raise
            )

    def test_valid_flat_plate_spec(
        self, carbon_material: Material, standard_layup: list[LayupLayer]
    ) -> None:
        spec = _make_spec(
            Topology.LAMINATE,
            GeometryType.FLAT_PLATE,
            AnalysisType.STATIC_TENSION,
            [carbon_material],
            layup=standard_layup,
        )
        assert spec.topology == Topology.LAMINATE

    def test_shell_spec_no_layup_required(self, aluminium_material: Material) -> None:
        spec = _make_spec(
            Topology.SHELL,
            GeometryType.CYLINDRICAL_SHELL,
            AnalysisType.PRESSURE,
            [aluminium_material],
            layup=[],
        )
        assert spec.topology == Topology.SHELL


# ---------------------------------------------------------------------------
# Validator tests
# ---------------------------------------------------------------------------

class TestCaseSpecValidator:
    """Tests for the business-rule validator (Layer A diagnostics)."""

    def setup_method(self) -> None:
        self.validator = CaseSpecValidator()

    def test_valid_flat_plate_tension_passes(
        self, carbon_material: Material, standard_layup: list[LayupLayer]
    ) -> None:
        spec = _make_spec(
            Topology.LAMINATE,
            GeometryType.FLAT_PLATE,
            AnalysisType.STATIC_TENSION,
            [carbon_material],
            layup=standard_layup,
        )
        result = self.validator.validate(spec)
        assert result.passed

    def test_wrong_topology_geometry_fails(
        self, aluminium_material: Material
    ) -> None:
        """SHELL topology with FLAT_PLATE geometry should fail."""
        spec = _make_spec(
            Topology.SHELL,            # wrong
            GeometryType.FLAT_PLATE,   # flat plate is a laminate family
            AnalysisType.PRESSURE,
            [aluminium_material],
        )
        result = self.validator.validate(spec)
        assert not result.passed
        assert any("topology" in e.lower() for e in result.errors)

    def test_forbidden_feature_fails(
        self, carbon_material: Material, standard_layup: list[LayupLayer]
    ) -> None:
        """Stiffener is forbidden on a flat plate."""
        spec = _make_spec(
            Topology.LAMINATE,
            GeometryType.FLAT_PLATE,
            AnalysisType.STATIC_TENSION,
            [carbon_material],
            layup=standard_layup,
        )
        spec.features = [Feature(name=FeatureName.STIFFENER, enabled=True)]
        result = self.validator.validate(spec)
        assert not result.passed

    def test_incompatible_analysis_fails(
        self, carbon_material: Material, standard_layup: list[LayupLayer]
    ) -> None:
        """Pressure analysis is not valid for flat_plate geometry."""
        spec = _make_spec(
            Topology.LAMINATE,
            GeometryType.FLAT_PLATE,
            AnalysisType.PRESSURE,    # not supported for flat_plate
            [carbon_material],
            layup=standard_layup,
        )
        result = self.validator.validate(spec)
        assert not result.passed

    def test_high_aspect_ratio_warns(
        self, carbon_material: Material, standard_layup: list[LayupLayer]
    ) -> None:
        spec = _make_spec(
            Topology.LAMINATE,
            GeometryType.FLAT_PLATE,
            AnalysisType.STATIC_TENSION,
            [carbon_material],
            layup=standard_layup,
        )
        spec.geometry = Geometry(
            geometry_type=GeometryType.FLAT_PLATE,
            length=2000.0, width=50.0, thickness=2.0   # aspect = 40 → warning
        )
        result = self.validator.validate(spec)
        assert result.passed           # still passes
        assert len(result.warnings) > 0
