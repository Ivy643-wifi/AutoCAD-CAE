"""Integration test: full pipeline in dry_run mode.

Dry run executes all stages except the actual solver (ccx).
Requires CadQuery and Gmsh to be installed.
"""

from __future__ import annotations

import pytest
from pathlib import Path


EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


@pytest.mark.integration
class TestPipelineDryRun:
    """Full pipeline integration tests (dry_run=True; no CCX required)."""

    @pytest.fixture(autouse=True)
    def _runs_dir(self, tmp_path: Path) -> None:
        self.runs_dir = tmp_path / "runs"

    def _run(self, yaml_name: str):
        from autocae.pipeline.runner import PipelineRunner
        runner = PipelineRunner(runs_dir=self.runs_dir, dry_run=True)
        return runner.run_from_yaml(EXAMPLES_DIR / yaml_name)

    def test_flat_plate_tension_dry_run(self) -> None:
        result = self._run("flat_plate_tension.yaml")
        assert result.success, result.error_message
        assert (result.run_dir / "case_spec.json").exists()
        assert (result.run_dir / "model.step").exists()
        assert (result.run_dir / "mesh.inp").exists()
        assert (result.run_dir / "analysis_model.json").exists()
        assert (result.run_dir / "solver_job.json").exists()
        assert (result.run_dir / "job.inp").exists()
        assert (result.run_dir / "diagnostics.json").exists()

    def test_open_hole_tension_dry_run(self) -> None:
        result = self._run("open_hole_tension.yaml")
        assert result.success, result.error_message

    def test_cylindrical_shell_pressure_dry_run(self) -> None:
        result = self._run("cylindrical_shell_pressure.yaml")
        assert result.success, result.error_message

    def test_flat_plate_buckling_dry_run(self) -> None:
        result = self._run("flat_plate_buckling.yaml")
        assert result.success, result.error_message
        # Buckling analysis should produce 2 steps (preload + buckle)
        import json
        am = json.loads((result.run_dir / "analysis_model.json").read_text())
        assert len(am["analysis_steps"]) == 2

    def test_run_dir_structure(self) -> None:
        result = self._run("flat_plate_tension.yaml")
        assert result.success
        expected_files = [
            "case_spec.json",
            "geometry_meta.json",
            "model.step",
            "mesh.inp",
            "mesh_groups.json",
            "mesh_quality_report.json",
            "analysis_model.json",
            "solver_job.json",
            "job.inp",
        ]
        for fname in expected_files:
            p = result.run_dir / fname
            assert p.exists(), f"Expected file missing: {fname}"
