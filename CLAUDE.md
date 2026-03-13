# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This repository contains:
1. **PDF design documents** — architecture and specification documents for the AutoCAE project.
2. **`autocae_pipeline/`** — Phase 1 MVP implementation of the automated CAD/CAE pipeline.

## PDF Documents

| File | Description |
|------|-------------|
| `CASESPEC_DESIGN_summary.pdf` | CaseSpec system design (structural hierarchy, template spec, Phase 1 minimum task set) |
| `主线2 - 01-软件 调研_对话.pdf` | Software research dialogue (tool selection rationale, MVP architecture) |
| `自动化CAD_CAE_Pipeline_工业级架构图与数据接口规范_V1.pdf` | Industrial architecture + data interface specification V1 (authoritative reference) |

## AutoCAE Pipeline — Project Structure

```
autocae_pipeline/
├── src/autocae/
│   ├── schemas/           # All Pydantic data interface models (the "contracts")
│   │   ├── case_spec.py   # CaseSpec — central pipeline input
│   │   ├── analysis_model.py  # AnalysisModel — solver-agnostic FE description
│   │   ├── mesh.py        # GeometryMeta, MeshGroups, MeshQualityReport
│   │   ├── solver.py      # SolverJob, RunStatus
│   │   └── postprocess.py # ResultSummary, FieldManifest, Diagnostics, ReviewReport
│   ├── case_spec/         # CaseSpec builder + validator (Layer A diagnostics)
│   ├── cad/               # CAD Builder (CadQuery) + 7 structural family templates
│   │   └── templates/     # flat_plate, open_hole_plate, cylindrical_shell,
│   │                      # laminated_beam, stringer_stiffened_panel,
│   │                      # sandwich_plate, bolted_lap_joint
│   ├── mesh/              # Mesh Builder (Gmsh integration)
│   ├── solver/            # Solver Adapter (CalculiX .inp writer) + SolverRunner
│   ├── postprocess/       # FRD parser + PostprocessEngine (plots, CSV, JSON)
│   ├── template_library/  # TemplateRegistry (15 Phase 1 templates)
│   │   ├── registry.py    #   TemplateRegistry.match() + list_templates() + get()
│   │   └── instantiator.py #  TemplateInstantiator: CaseSpec+Template → AnalysisModel
│   ├── diagnostics/       # Four-layer diagnostics validator (DiagnosticsValidator)
│   ├── pipeline/          # PipelineRunner — main orchestrator
│   └── cli.py             # Typer CLI (autocae run/validate/list-templates)
├── examples/              # YAML case specification examples
├── tests/                 # pytest unit + integration tests
└── pyproject.toml
```

## Commands

All commands run from `autocae_pipeline/`.

```bash
# Install
pip install -e ".[dev]"

# Run the full pipeline (dry_run skips actual CCX execution)
autocae run examples/flat_plate_tension.yaml --dry-run

# Validate a case spec file only
autocae validate examples/flat_plate_tension.yaml

# List all registered Phase 1 templates
autocae list-templates

# Run all tests
pytest tests/

# Run a single test file or test by name
pytest tests/test_case_spec.py
pytest tests/test_case_spec.py::TestCaseSpecValidator::test_valid_flat_plate_tension_passes

# Run only unit tests (no Gmsh/CadQuery required)
pytest tests/ -m "not integration"

# Run integration tests (requires CadQuery + Gmsh installed)
pytest tests/ -m integration

# Lint (line-length 100, selects E,F,I,N,UP,ANN)
ruff check src/ tests/
ruff format src/ tests/

# Type check (strict mode)
mypy src/
```

## Pipeline Architecture

**Main flow (Phase 1 MVP):**
```
CaseSpec → TemplateMatch → CAD (CadQuery) → STEP → Mesh (Gmsh) →
AnalysisModel → Solver Adapter → CalculiX → Postprocess → Outputs
```

**File-interface driven (G-11):** Every stage communicates via files in the run directory:
- `case_spec.json` → `model.step` + `geometry_meta.json` → `mesh.inp` + `mesh_groups.json`
  → `analysis_model.json` → `job.inp` → `job.frd` → `result_summary.json`

**Run directory layout:** `runs/<case_id>/` contains all intermediate and final outputs.

## Key Design Decisions (from architecture spec)

| Rule | Description |
|------|-------------|
| G-01 | Pipeline is solver-agnostic; `CalculiXAdapter` is swappable |
| G-02 | CAD dual-track: CadQuery primary, external STEP accepted |
| G-03 | Geometry exchange format is STEP only |
| G-04 | Template-first: `TemplateRegistry.match()` before building from scratch |
| G-09 | Only Review Gate-approved cases write back to the library |
| G-11 | Module decoupling via file interfaces (JSON, STEP, .inp, .frd) |

## CaseSpec Structural Hierarchy

```
Topology → Family (GeometryType) → Analysis Case
laminate → flat_plate              → tension, compression, bending, buckling
laminate → open_hole_plate         → tension, compression
shell    → cylindrical_shell       → pressure, buckling
beam     → laminated_beam          → bending, torsion
panel    → stringer_stiffened_panel → buckling, tension
sandwich → sandwich_plate          → bending, shear
joint    → bolted_lap_joint        → tension, shear
```

## Data Interface Objects

All defined as Pydantic v2 models in `src/autocae/schemas/`. Key objects:
- `CaseSpec` — problem definition (NOT the solver deck)
- `AnalysisModel` — 3-layer solver-agnostic FE model (Problem Def + Canonical + SolverExtensions)
- `MeshGroups` — Gmsh physical group → solver set mapping
- `ResultSummary` — scalar key results (max_displacement, max_mises_stress, etc.)
- `Diagnostics` — 4-layer validation: input → interface → runtime → repair suggestions

**Validation split:** `CaseSpecValidator` (in `case_spec/validator.py`) runs Layer A only (schema + business rules). `DiagnosticsValidator` (in `diagnostics/validator.py`) covers all four layers (A–D) including interface file checks, solver log parsing, and repair suggestions.

## Adding a New Structural Family

1. Create `src/autocae/cad/templates/<family>.py` — subclass `BaseCADTemplate`, implement `geometry_type` property and `build(spec, output_dir) -> CADResult`.
2. Register templates in `src/autocae/template_library/registry.py`.
3. Extend `TemplateInstantiator` in `template_library/instantiator.py` to map the new template → `AnalysisModel` steps.
4. Add a `GeometryType` entry and topology mapping in `schemas/case_spec.py`.
5. Add YAML examples in `examples/` and integration tests.

## Technology Stack

- **CAD:** CadQuery (parametric, code-driven; FreeCAD for optional visual inspection)
- **Mesh:** Gmsh (OpenCASCADE kernel; STEP import; Physical Groups)
- **Solver:** CalculiX CCX (Abaqus-style .inp format)
- **Visualization:** PyVista (FE field clouds), matplotlib (curves)
- **Data validation:** Pydantic v2
- **CLI:** Typer + Rich
