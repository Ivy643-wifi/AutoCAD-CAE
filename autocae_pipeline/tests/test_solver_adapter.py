"""Tests for the CalculiX Solver Adapter (.inp file generation)."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from autocae.schemas.analysis_model import (
    AnalysisModel,
    AnalysisStep,
    AnalysisStepType,
    BoundaryConditionDef,
    CanonicalMaterial,
    LoadDef,
    Metadata,
    OutputRequestDef,
    Section,
    SectionType,
    SolverExtensions,
    TemplateLinkage,
)
from autocae.schemas.mesh import MeshGroup, MeshGroups
from autocae.backend.services.solver_service import CalculiXAdapter


def make_simple_analysis_model() -> AnalysisModel:
    return AnalysisModel(
        metadata=Metadata(case_spec_id="test_case"),
        geometry_file="model.step",
        geometry_meta_file="geometry_meta.json",
        materials=[CanonicalMaterial(
            material_id="mat1", name="Carbon",
            E1=135000.0, E2=10000.0, G12=5200.0, nu12=0.3
        )],
        sections=[Section(
            section_id="sec1", section_type=SectionType.SOLID,
            region_ref="SOLID", material_id="mat1", thickness=2.0
        )],
        loads=[LoadDef(
            load_id="ld1", load_type="tension",
            set_ref="LOAD_END", magnitude=1000.0, direction=[1.0, 0.0, 0.0]
        )],
        boundary_conditions=[BoundaryConditionDef(
            bc_id="bc1", set_ref="FIXED_END",
            constrained_dofs=[1, 2, 3, 4, 5, 6],
            displacement_values={i: 0.0 for i in range(1, 7)},
        )],
        analysis_steps=[AnalysisStep(
            step_id="step1",
            step_type=AnalysisStepType.STATIC,
            step_name="tension_step",
        )],
        output_requests=OutputRequestDef(),
        solver_extensions=SolverExtensions(),
    )


def make_mesh_groups(mesh_file: str) -> MeshGroups:
    return MeshGroups(
        geometry_id="geo1",
        mesh_file=mesh_file,
        groups=[
            MeshGroup(group_id="g1", entity_type="surface", gmsh_tag=1,
                      mapped_region="FIXED_END", solver_set_name="FIXED_END"),
            MeshGroup(group_id="g2", entity_type="surface", gmsh_tag=2,
                      mapped_region="LOAD_END", solver_set_name="LOAD_END"),
            MeshGroup(group_id="g3", entity_type="volume", gmsh_tag=3,
                      mapped_region="SOLID", solver_set_name="SOLID"),
        ],
    )


class TestCalculiXAdapter:
    def setup_method(self) -> None:
        self.adapter = CalculiXAdapter()

    def test_write_input_creates_file(self, tmp_path: Path) -> None:
        model = make_simple_analysis_model()
        groups = make_mesh_groups(str(tmp_path / "mesh.inp"))
        files = self.adapter.write_input(model, groups, tmp_path)
        assert len(files) == 1
        assert files[0].exists()

    def test_inp_contains_material_block(self, tmp_path: Path) -> None:
        model = make_simple_analysis_model()
        groups = make_mesh_groups(str(tmp_path / "mesh.inp"))
        files = self.adapter.write_input(model, groups, tmp_path)
        content = files[0].read_text(encoding="utf-8")
        assert "*MATERIAL" in content
        assert "CARBON" in content.upper()

    def test_inp_contains_step_block(self, tmp_path: Path) -> None:
        model = make_simple_analysis_model()
        groups = make_mesh_groups(str(tmp_path / "mesh.inp"))
        files = self.adapter.write_input(model, groups, tmp_path)
        content = files[0].read_text(encoding="utf-8")
        assert "*STEP" in content
        assert "*STATIC" in content
        assert "*END STEP" in content

    def test_inp_contains_boundary(self, tmp_path: Path) -> None:
        model = make_simple_analysis_model()
        groups = make_mesh_groups(str(tmp_path / "mesh.inp"))
        files = self.adapter.write_input(model, groups, tmp_path)
        content = files[0].read_text(encoding="utf-8")
        assert "*BOUNDARY" in content
        assert "FIXED_END" in content

    def test_solver_job_written(self, tmp_path: Path) -> None:
        model = make_simple_analysis_model()
        groups = make_mesh_groups(str(tmp_path / "mesh.inp"))
        files = self.adapter.write_input(model, groups, tmp_path)
        job = self.adapter.build_solver_job(model, files, tmp_path)
        assert (tmp_path / "solver_job.json").exists()
        assert job.solver_type.value == "calculix"

    def test_buckling_step_keyword(self, tmp_path: Path) -> None:
        model = make_simple_analysis_model()
        model.analysis_steps = [
            AnalysisStep(step_id="s1", step_type=AnalysisStepType.STATIC, step_name="pre"),
            AnalysisStep(step_id="s2", step_type=AnalysisStepType.BUCKLE,
                         step_name="buckle", num_eigenmodes=5),
        ]
        groups = make_mesh_groups(str(tmp_path / "mesh.inp"))
        files = self.adapter.write_input(model, groups, tmp_path)
        content = files[0].read_text(encoding="utf-8")
        assert "*BUCKLE" in content
