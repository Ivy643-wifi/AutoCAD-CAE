"""Data interface schemas for the AutoCAE pipeline.

All inter-module data objects are defined here as Pydantic models,
following the industrial architecture specification V1.
"""

from autocae.schemas.case_spec import (
    AnalysisType,
    BoundaryCondition,
    CaseSpec,
    Feature,
    Geometry,
    GeometryType,
    Load,
    Material,
    MeshPreferences,
    OutputRequest,
    Topology,
)
from autocae.schemas.analysis_model import (
    AnalysisModel,
    AnalysisStep,
    AnalysisStepType,
    BoundaryConditionDef,
    CanonicalMaterial,
    Contact,
    LoadDef,
    Metadata,
    OutputRequestDef,
    Region,
    Section,
    Set,
    SolverExtensions,
    TemplateLinkage,
)
from autocae.schemas.mesh import (
    GeometryMeta,
    MeshGroup,
    MeshGroups,
    MeshQualityReport,
)
from autocae.schemas.solver import (
    RunStatus,
    RunStatusEnum,
    SolverJob,
    SolverType,
)
from autocae.schemas.postprocess import (
    Diagnostics,
    FieldManifest,
    FieldResult,
    LibraryUpdateRequest,
    ResultSummary,
    ReviewReport,
    ReviewStatus,
)

__all__ = [
    # case_spec
    "AnalysisType",
    "BoundaryCondition",
    "CaseSpec",
    "Feature",
    "Geometry",
    "GeometryType",
    "Load",
    "Material",
    "MeshPreferences",
    "OutputRequest",
    "Topology",
    # analysis_model
    "AnalysisModel",
    "AnalysisStep",
    "AnalysisStepType",
    "BoundaryConditionDef",
    "CanonicalMaterial",
    "Contact",
    "LoadDef",
    "Metadata",
    "OutputRequestDef",
    "Region",
    "Section",
    "Set",
    "SolverExtensions",
    "TemplateLinkage",
    # mesh
    "GeometryMeta",
    "MeshGroup",
    "MeshGroups",
    "MeshQualityReport",
    # solver
    "RunStatus",
    "RunStatusEnum",
    "SolverJob",
    "SolverType",
    # postprocess
    "Diagnostics",
    "FieldManifest",
    "FieldResult",
    "LibraryUpdateRequest",
    "ResultSummary",
    "ReviewReport",
    "ReviewStatus",
]
