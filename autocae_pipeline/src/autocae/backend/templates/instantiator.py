"""Template Instantiator — CaseTemplate + CaseSpec → AnalysisModel。"""

from __future__ import annotations

import uuid
from pathlib import Path

from loguru import logger

from autocae.schemas.analysis_model import (
    AnalysisModel,
    AnalysisStep,
    AnalysisStepType,
    BoundaryConditionDef,
    CanonicalMaterial,
    LayupPly,
    LoadDef,
    Metadata,
    OutputRequestDef,
    Region,
    Section,
    SectionType,
    Set,
    SolverExtensions,
    TemplateLinkage,
)
from autocae.schemas.case_spec import (
    AnalysisType,
    BoundaryType,
    CaseSpec,
    LoadType,
    Material,
)
from autocae.backend.templates.registry import CaseTemplate


_ANALYSIS_TYPE_TO_STEP: dict[AnalysisType, AnalysisStepType] = {
    AnalysisType.STATIC_TENSION:     AnalysisStepType.STATIC,
    AnalysisType.STATIC_COMPRESSION: AnalysisStepType.STATIC,
    AnalysisType.BENDING:            AnalysisStepType.STATIC,
    AnalysisType.SHEAR:              AnalysisStepType.STATIC,
    AnalysisType.TORSION:            AnalysisStepType.STATIC,
    AnalysisType.PRESSURE:           AnalysisStepType.STATIC,
    AnalysisType.BUCKLING:           AnalysisStepType.BUCKLE,
    AnalysisType.MODAL:              AnalysisStepType.FREQUENCY,
    AnalysisType.IMPACT:             AnalysisStepType.DYNAMIC,
    AnalysisType.FATIGUE:            AnalysisStepType.STATIC,
    AnalysisType.THERMAL:            AnalysisStepType.HEAT_TRANSFER,
}


class TemplateInstantiator:
    """从 CaseTemplate + CaseSpec 实例化 AnalysisModel。"""

    def instantiate(
        self,
        spec: CaseSpec,
        template: CaseTemplate | None,
        geometry_file: str,
        geometry_meta_file: str,
    ) -> AnalysisModel:
        source = "template" if template else "new_build"
        logger.info(
            f"TemplateInstantiator: building AnalysisModel "
            f"(source={source}, analysis={spec.analysis_type.value})"
        )

        metadata = Metadata(
            case_spec_id=spec.metadata.case_id,
            source=source,
        )
        linkage = TemplateLinkage(
            template_id=template.template_id if template else None,
            template_version=template.version if template else None,
            instantiation_params={
                "geometry_type": spec.geometry.geometry_type.value,
                "analysis_type": spec.analysis_type.value,
            },
        )

        can_materials = [self._convert_material(m) for m in spec.materials]
        regions, sets = self._build_regions_sets(spec)
        sections = self._build_sections(spec)
        loads = self._build_loads(spec)
        bcs = self._build_bcs(spec)
        step_type = _ANALYSIS_TYPE_TO_STEP.get(spec.analysis_type, AnalysisStepType.STATIC)
        steps = self._build_steps(spec, step_type)
        or_ = OutputRequestDef(
            field_outputs=spec.output_requests.field_outputs,
            history_outputs=spec.output_requests.history_outputs,
        )

        return AnalysisModel(
            metadata=metadata,
            template_linkage=linkage,
            geometry_file=geometry_file,
            geometry_meta_file=geometry_meta_file,
            regions=regions,
            sets=sets,
            materials=can_materials,
            sections=sections,
            loads=loads,
            boundary_conditions=bcs,
            analysis_steps=steps,
            output_requests=or_,
            solver_extensions=SolverExtensions(),
        )

    def _convert_material(self, mat: Material) -> CanonicalMaterial:
        return CanonicalMaterial(
            material_id=mat.material_id,
            name=mat.name,
            E=mat.E, nu=mat.nu, rho=mat.rho,
            E1=mat.E1, E2=mat.E2, G12=mat.G12, nu12=mat.nu12,
        )

    def _build_regions_sets(self, spec: CaseSpec) -> tuple[list[Region], list[Set]]:
        from autocae.schemas.analysis_model import EntityType

        regions: list[Region] = []
        sets: list[Set] = []
        locations = set()
        for bc in spec.boundary_conditions:
            locations.add(bc.location.upper())
        for ld in spec.loads:
            locations.add(ld.location.upper())

        for loc in locations:
            rid = f"region_{loc.lower()}"
            regions.append(Region(
                region_id=rid,
                entity_type=EntityType.SURFACE,
                description=f"Named region: {loc}",
            ))
            sets.append(Set(
                set_id=f"set_{loc.lower()}",
                set_type="surface",
                region_ref=rid,
                solver_set_name=loc,
            ))
        return regions, sets

    def _build_sections(self, spec: CaseSpec) -> list[Section]:
        if not spec.materials:
            return []
        mat = spec.materials[0]
        if spec.layup:
            return [Section(
                section_id="sec_composite",
                section_type=SectionType.COMPOSITE_SHELL,
                region_ref="SOLID",
                material_id=mat.material_id,
                layup=[
                    LayupPly(
                        angle=lay.angle,
                        thickness=lay.thickness,
                        material_id=lay.material_id if lay.material_id != "default"
                                    else mat.material_id,
                    )
                    for lay in spec.layup
                ],
            )]
        else:
            return [Section(
                section_id="sec_solid",
                section_type=SectionType.SOLID,
                region_ref="SOLID",
                material_id=mat.material_id,
                thickness=spec.geometry.thickness,
            )]

    def _build_loads(self, spec: CaseSpec) -> list[LoadDef]:
        return [
            LoadDef(
                load_id=ld.load_id,
                load_type=ld.load_type.value,
                set_ref=ld.location.upper(),
                magnitude=ld.magnitude,
                direction=ld.direction,
            )
            for ld in spec.loads
        ]

    def _build_bcs(self, spec: CaseSpec) -> list[BoundaryConditionDef]:
        bcs: list[BoundaryConditionDef] = []
        for bc in spec.boundary_conditions:
            constrained = bc.constrained_dofs
            disp_vals: dict[int, float] = {}
            if bc.bc_type in (BoundaryType.FIXED, BoundaryType.ENCASTRE):
                constrained = list(range(1, 7))
                disp_vals = {i: 0.0 for i in range(1, 7)}
            elif bc.bc_type == BoundaryType.PINNED:
                constrained = [1, 2, 3]
                disp_vals = {1: 0.0, 2: 0.0, 3: 0.0}
            elif bc.bc_type == BoundaryType.SIMPLY_SUPPORTED:
                constrained = [3]
                disp_vals = {3: 0.0}
            bcs.append(BoundaryConditionDef(
                bc_id=bc.bc_id,
                set_ref=bc.location.upper(),
                constrained_dofs=constrained,
                displacement_values=disp_vals,
            ))
        return bcs

    def _build_steps(
        self, spec: CaseSpec, step_type: AnalysisStepType
    ) -> list[AnalysisStep]:
        step = AnalysisStep(
            step_id=f"step_{uuid.uuid4().hex[:6]}",
            step_type=step_type,
            step_name=f"{spec.analysis_type.value}_step",
            nlgeom=(spec.analysis_type == AnalysisType.BUCKLING),
        )
        if step_type == AnalysisStepType.BUCKLE:
            pre_step = AnalysisStep(
                step_id=f"step_preload_{uuid.uuid4().hex[:6]}",
                step_type=AnalysisStepType.STATIC,
                step_name="preload_step",
                nlgeom=False,
            )
            return [pre_step, step]
        if step_type == AnalysisStepType.FREQUENCY:
            step.num_frequencies = 10
            return [step]
        return [step]
