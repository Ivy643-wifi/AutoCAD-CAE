"""Template Instantiator — 将 CaseTemplate + CaseSpec 转换为 AnalysisModel。

这是 CaseSpec（问题定义）到 AnalysisModel（有限元描述）的桥梁。

核心逻辑：
    1. 将 CaseSpec.materials → CanonicalMaterial（统一格式）
    2. 从 BC/Load 的 location 名推导出 Region 和 Set（命名集合）
    3. 根据铺层存在与否选择截面类型（COMPOSITE_SHELL vs SOLID）
    4. 根据 AnalysisType 映射到 AnalysisStepType，并构建分析步列表：
        - 屈曲分析：两步（preload_step + buckle_step）
        - 模态分析：一步（frequency_step，默认 10 阶）
        - 其他：一步（static_step）
"""

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
from autocae.template_library.registry import CaseTemplate


# 分析类型 → 分析步类型的映射表
# 大多数分析（静力、弯曲、剪切、扭转、压力）都用 STATIC 步
# 屈曲用 BUCKLE，模态用 FREQUENCY，冲击用 DYNAMIC
_ANALYSIS_TYPE_TO_STEP: dict[AnalysisType, AnalysisStepType] = {
    AnalysisType.STATIC_TENSION:    AnalysisStepType.STATIC,
    AnalysisType.STATIC_COMPRESSION: AnalysisStepType.STATIC,
    AnalysisType.BENDING:           AnalysisStepType.STATIC,
    AnalysisType.SHEAR:             AnalysisStepType.STATIC,
    AnalysisType.TORSION:           AnalysisStepType.STATIC,
    AnalysisType.PRESSURE:          AnalysisStepType.STATIC,
    AnalysisType.BUCKLING:          AnalysisStepType.BUCKLE,    # 屈曲特征值步
    AnalysisType.MODAL:             AnalysisStepType.FREQUENCY, # 频率步
    AnalysisType.IMPACT:            AnalysisStepType.DYNAMIC,
    AnalysisType.FATIGUE:           AnalysisStepType.STATIC,
    AnalysisType.THERMAL:           AnalysisStepType.HEAT_TRANSFER,
}


class TemplateInstantiator:
    """从 CaseTemplate + CaseSpec 实例化 AnalysisModel。

    使用方式：
        instantiator = TemplateInstantiator()
        model = instantiator.instantiate(spec, template, geometry_file, geo_meta_file)
    """

    def instantiate(
        self,
        spec: CaseSpec,
        template: CaseTemplate | None,
        geometry_file: str,
        geometry_meta_file: str,
    ) -> AnalysisModel:
        """构建 AnalysisModel。

        有模板时：source="template"，TemplateLinkage 记录模板 ID。
        无模板时：source="new_build"，完全从 CaseSpec 推导。

        Args:
            spec:               已验证的 CaseSpec
            template:           匹配到的 CaseTemplate（可为 None）
            geometry_file:      model.step 的路径
            geometry_meta_file: geometry_meta.json 的路径

        Returns:
            可直接传给 SolverAdapter 的 AnalysisModel
        """
        source = "template" if template else "new_build"
        logger.info(
            f"TemplateInstantiator: building AnalysisModel "
            f"(source={source}, analysis={spec.analysis_type.value})"
        )

        # 元信息：记录与 CaseSpec 的追溯链
        metadata = Metadata(
            case_spec_id=spec.metadata.case_id,
            source=source,
        )

        # 模板关联（记录实例化来源）
        linkage = TemplateLinkage(
            template_id=template.template_id if template else None,
            template_version=template.version if template else None,
            instantiation_params={
                "geometry_type": spec.geometry.geometry_type.value,
                "analysis_type": spec.analysis_type.value,
            },
        )

        # 步骤 1：转换材料（CaseSpec.Material → CanonicalMaterial）
        can_materials = [self._convert_material(m) for m in spec.materials]

        # 步骤 2：从 BC/Load 的 location 名推导命名区域和求解器集合
        regions, sets = self._build_regions_sets(spec)

        # 步骤 3：构建截面（自动选择 COMPOSITE_SHELL 或 SOLID）
        sections = self._build_sections(spec)

        # 步骤 4：转换载荷（CaseSpec.Load → LoadDef）
        loads = self._build_loads(spec)

        # 步骤 5：转换边界条件（CaseSpec.BoundaryCondition → BoundaryConditionDef）
        bcs = self._build_bcs(spec)

        # 步骤 6：构建分析步（根据分析类型决定步数和类型）
        step_type = _ANALYSIS_TYPE_TO_STEP.get(spec.analysis_type, AnalysisStepType.STATIC)
        steps = self._build_steps(spec, step_type)

        # 步骤 7：输出请求（直接透传 CaseSpec 的 output_requests）
        or_ = OutputRequestDef(
            field_outputs=spec.output_requests.field_outputs,
            history_outputs=spec.output_requests.history_outputs,
        )

        model = AnalysisModel(
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

        return model

    # ------------------------------------------------------------------
    # 私有辅助方法
    # ------------------------------------------------------------------

    def _convert_material(self, mat: Material) -> CanonicalMaterial:
        """将 CaseSpec 材料转换为规范材料（字段直接映射）。"""
        return CanonicalMaterial(
            material_id=mat.material_id,
            name=mat.name,
            E=mat.E, nu=mat.nu, rho=mat.rho,
            E1=mat.E1, E2=mat.E2, G12=mat.G12, nu12=mat.nu12,
        )

    def _build_regions_sets(self, spec: CaseSpec) -> tuple[list[Region], list[Set]]:
        """从边界条件和载荷的 location 名创建命名区域和集合。

        例如：BC location="FIXED_END" + Load location="LOAD_END"
              → 生成 Region(region_fixed_end) + Set(set_fixed_end, solver_set_name="FIXED_END")
                 Region(region_load_end) + Set(set_load_end, solver_set_name="LOAD_END")

        这些集合名会在 job.inp 的 *BOUNDARY 和 *CLOAD 指令中引用。
        """
        from autocae.schemas.analysis_model import EntityType

        regions: list[Region] = []
        sets: list[Set] = []

        # 收集所有唯一的位置名称（大写）
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
                solver_set_name=loc,  # 大写，用于 job.inp
            ))

        return regions, sets

    def _build_sections(self, spec: CaseSpec) -> list[Section]:
        """根据是否有铺层定义选择截面类型。

        有铺层（LAMINATE topology）→ COMPOSITE_SHELL 截面（*SHELL SECTION, COMPOSITE）
        无铺层（SHELL/BEAM 等）    → SOLID 截面（*SOLID SECTION）
        """
        if not spec.materials:
            return []

        mat = spec.materials[0]  # 取第一个材料作为主材料

        if spec.layup:
            # 层合板：使用复合材料壳截面
            return [Section(
                section_id="sec_composite",
                section_type=SectionType.COMPOSITE_SHELL,
                region_ref="SOLID",
                material_id=mat.material_id,
                layup=[
                    LayupPly(
                        angle=lay.angle,
                        thickness=lay.thickness,
                        # 若铺层材料 ID 为 "default"，回退到第一个材料
                        material_id=lay.material_id if lay.material_id != "default"
                                    else mat.material_id,
                    )
                    for lay in spec.layup
                ],
            )]
        else:
            # 各向同性材料：使用实体截面
            return [Section(
                section_id="sec_solid",
                section_type=SectionType.SOLID,
                region_ref="SOLID",
                material_id=mat.material_id,
                thickness=spec.geometry.thickness,
            )]

    def _build_loads(self, spec: CaseSpec) -> list[LoadDef]:
        """将 CaseSpec 载荷列表转换为规范 LoadDef 列表。"""
        return [
            LoadDef(
                load_id=ld.load_id,
                load_type=ld.load_type.value,
                set_ref=ld.location.upper(),   # 集合名必须大写
                magnitude=ld.magnitude,
                direction=ld.direction,
            )
            for ld in spec.loads
        ]

    def _build_bcs(self, spec: CaseSpec) -> list[BoundaryConditionDef]:
        """将 CaseSpec 边界条件转换为规范 BoundaryConditionDef 列表。

        边界条件类型到 DOF 约束的映射：
            FIXED/ENCASTRE：全约束 DOF 1-6（位移+转动全为 0）
            PINNED：约束平动 DOF 1-3（转动自由）
            SIMPLY_SUPPORTED：仅约束法向位移 DOF 3
        """
        bcs: list[BoundaryConditionDef] = []
        for bc in spec.boundary_conditions:
            constrained = bc.constrained_dofs
            disp_vals: dict[int, float] = {}
            if bc.bc_type == BoundaryType.FIXED or bc.bc_type == BoundaryType.ENCASTRE:
                constrained = list(range(1, 7))      # DOF 1~6 全约束
                disp_vals = {i: 0.0 for i in range(1, 7)}
            elif bc.bc_type == BoundaryType.PINNED:
                constrained = [1, 2, 3]              # 仅平动约束
                disp_vals = {1: 0.0, 2: 0.0, 3: 0.0}
            elif bc.bc_type == BoundaryType.SIMPLY_SUPPORTED:
                constrained = [3]                    # 仅法向约束
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
        """构建分析步列表。

        特殊处理：
            屈曲分析（BUCKLE）：需要 2 步
                Step 1：preload_step（静力预载，施加边界条件和载荷）
                Step 2：buckle_step（特征值屈曲求解）
                原因：CalculiX 的 *BUCKLE 步需要一个前置静力步来建立刚度矩阵。

            模态分析（FREQUENCY）：1 步，默认提取 10 阶频率。

            其他：1 步静力分析。
        """
        step = AnalysisStep(
            step_id=f"step_{uuid.uuid4().hex[:6]}",
            step_type=step_type,
            step_name=f"{spec.analysis_type.value}_step",
            nlgeom=(spec.analysis_type == AnalysisType.BUCKLING),  # 屈曲用大变形
        )
        if step_type == AnalysisStepType.BUCKLE:
            # 屈曲：先加一个预载静力步
            pre_step = AnalysisStep(
                step_id=f"step_preload_{uuid.uuid4().hex[:6]}",
                step_type=AnalysisStepType.STATIC,
                step_name="preload_step",
                nlgeom=False,
            )
            return [pre_step, step]   # 两步：预载 + 屈曲
        if step_type == AnalysisStepType.FREQUENCY:
            step.num_frequencies = 10  # 默认提取前 10 阶模态
            return [step]
        return [step]   # 其他：单步静力分析
