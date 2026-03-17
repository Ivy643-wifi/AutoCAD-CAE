# Run Contract v1 (M1.9)

This document freezes the run directory artifact contract for AutoCAE Phase 1.

## Canonical Layout (preferred)

For each run, artifacts should be written under:

`runs/<case_id>/`

Expected canonical files:

- `case_spec.json`
- `model.step`
- `geometry_meta.json`
- `mesh.inp`
- `mesh_groups.json`
- `mesh_quality_report.json`
- `analysis_model.json`
- `solver_job.json`
- `job.inp`
- `run_status.json`
- `review_transcript.json`
- `issue_report.json`

## Legacy Compatibility Layout (read-only compatibility)

For backward compatibility, the system also resolves legacy paths:

- `02_cad/model.step`
- `02_cad/geometry_meta.json`
- `03_mesh/mesh.inp`
- `03_mesh/mesh_groups.json`
- `03_mesh/mesh_quality_report.json`
- `04_analysis_model/analysis_model.json`
- `05_solver_input/solver_job.json`
- `05_solver_input/job.inp`
- `06_solver/run_status.json`
- `06_solver/job.frd`

## Artifact Locator

All compatibility reads should go through:

- `autocae.backend.orchestrator.artifact_locator.ArtifactLocator`

This ensures:

- A single source of truth for artifact candidates.
- Root-first lookup with legacy fallback.
- Consistent behavior across `solve`, `visualize`, and `review`.
