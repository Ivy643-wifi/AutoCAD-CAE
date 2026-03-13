"""Analysis Model data schema.

analysis_model.json — 统一分析模型对象。
生产者：TemplateInstantiator（由 CaseSpec + CaseTemplate 生成）。
消费者：SolverAdapter（生成求解器输入卡片），PostprocessEngine（解析结果时参考）。

设计原则：与求解器无关（Solver-agnostic）。采用三层结构：
  Layer 1 – 问题定义层   (来自 CaseSpec，描述物理问题)
  Layer 2 – 规范分析模型 (与求解器无关的有限元描述)
  Layer 3 – 求解器扩展层 (特定求解器的覆盖参数，如 CalculiX 线程数)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AnalysisStepType(str, Enum):
    """分析步类型：对应求解器中的 *STEP 关键字类型。"""
    STATIC = "static"                              # 静力分析 (*STATIC)
    BUCKLE = "buckle"                              # 线性屈曲分析 (*BUCKLE)
    FREQUENCY = "frequency"                        # 自由振动频率分析 (*FREQUENCY)
    DYNAMIC = "dynamic"                            # 显式/隐式动力学 (*DYNAMIC)
    HEAT_TRANSFER = "heat_transfer"                # 热传导 (*HEAT TRANSFER)
    COUPLED_TEMP_DISPLACEMENT = "coupled_temp_displacement"  # 热力耦合


class EntityType(str, Enum):
    """几何实体类型：区分面/边/点/体。"""
    SURFACE = "surface"  # 面（对应 Gmsh dim=2）
    EDGE = "edge"        # 边（对应 Gmsh dim=1）
    VERTEX = "vertex"    # 点（对应 Gmsh dim=0）
    VOLUME = "volume"    # 体（对应 Gmsh dim=3）


class SectionType(str, Enum):
    """截面类型：决定求解器使用什么单元/截面关键字。"""
    SHELL = "shell"                    # 壳截面 (*SHELL SECTION)
    SOLID = "solid"                    # 实体截面 (*SOLID SECTION)
    BEAM = "beam"                      # 梁截面 (*BEAM SECTION)
    COMPOSITE_SHELL = "composite_shell"  # 复合材料壳截面 (*SHELL SECTION, COMPOSITE)


# ---------------------------------------------------------------------------
# Sub-models  子模型
# ---------------------------------------------------------------------------

class Metadata(BaseModel):
    """分析模型元信息。"""
    analysis_id: str = Field(default_factory=lambda: f"am_{uuid.uuid4().hex[:8]}")
    version: str = "1.0"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    source: str = "template"    # "template"（由模板生成）或 "new_build"（从零构建）
    status: str = "active"
    case_spec_id: str | None = None  # 对应的 CaseSpec.case_id（追溯链）
    notes: str = ""


class TemplateLinkage(BaseModel):
    """模板关联信息：记录该分析模型是由哪个模板实例化的。"""
    template_id: str | None = None
    template_version: str | None = None
    instantiation_params: dict[str, Any] = Field(default_factory=dict)  # 实例化时的参数快照


class Region(BaseModel):
    """命名几何区域：将 STEP 几何中的面/边映射为有工程含义的名称。

    例如：FIXED_END（固定端面），LOAD_END（加载端面）。
    这些名称在后续的边界条件和载荷施加中作为引用键。
    """
    region_id: str
    entity_type: EntityType
    description: str = ""
    step_entities: list[str] = Field(
        default_factory=list,
        description="该区域包含的 STEP 实体标签（面/边）",
    )


class Set(BaseModel):
    """命名节点/单元集合：求解器输入中的 NSET/ELSET/SURFACE。

    solver_set_name 就是在 job.inp 文件里出现的集合名，如 'FIXED_END'。
    """
    set_id: str
    set_type: str = Field(description="集合类型：'node' | 'element' | 'surface'")
    region_ref: str = Field(description="引用的 Region.region_id")
    solver_set_name: str = Field(
        description="求解器输入文件中的集合名称（大写，如 'FIXED_END'）",
    )


class CanonicalMaterial(BaseModel):
    """规范材料：Layer 2 中与求解器无关的材料表达。

    同时支持各向同性（E, nu）和正交各向异性（E1, E2, G12, ...）。
    CalculiXAdapter 会根据非 None 的字段自动选择 *ELASTIC 或 *ELASTIC, TYPE=ORTHO。
    """
    material_id: str
    name: str
    # 各向同性
    E: float | None = None
    nu: float | None = None
    rho: float | None = None
    # 正交各向异性（复合材料）
    E1: float | None = None
    E2: float | None = None
    E3: float | None = None   # 厚度方向（通常 ≈ E2）
    G12: float | None = None
    G13: float | None = None
    G23: float | None = None
    nu12: float | None = None
    nu13: float | None = None
    nu23: float | None = None


class LayupPly(BaseModel):
    """铺层（在 AnalysisModel 层中的表达）：已解析为规范格式。"""
    angle: float      # 纤维角度 [deg]
    thickness: float  # 层厚 [mm]
    material_id: str  # 引用 CanonicalMaterial.material_id


class Section(BaseModel):
    """截面属性：将材料和截面信息分配给几何区域。

    对应求解器中的 *SHELL SECTION / *SOLID SECTION / *COMPOSITE SHELL SECTION。
    region_ref 必须与 MeshGroups 中的 solver_set_name 一致（大写）。
    """
    section_id: str
    section_type: SectionType
    region_ref: str      # 对应求解器集合名（如 'SOLID'）
    material_id: str     # 对应 CanonicalMaterial.material_id
    thickness: float | None = None        # 壳单元厚度 [mm]
    layup: list[LayupPly] = Field(default_factory=list)  # 复合材料壳铺层序列


class LoadDef(BaseModel):
    """载荷定义（Layer 2 表达）：规范化的载荷描述，与求解器无关。"""
    load_id: str
    load_type: str          # 载荷类型字符串（来自 CaseSpec.LoadType.value）
    set_ref: str            # 施加载荷的集合名（大写，如 'LOAD_END'）
    magnitude: float        # 载荷量值
    direction: list[float] = Field(default_factory=lambda: [1.0, 0.0, 0.0])  # 方向向量
    amplitude: str | None = None  # 载荷幅值（如 "RAMP"）


class BoundaryConditionDef(BaseModel):
    """边界条件定义（Layer 2 表达）：规范化的约束描述。

    constrained_dofs: 被约束的 DOF 列表（1=Ux,2=Uy,3=Uz,4=Rx,5=Ry,6=Rz）
    displacement_values: 各 DOF 的指定位移值（通常为 0.0 表示固定）
    """
    bc_id: str
    set_ref: str                              # 施加约束的集合名（大写）
    constrained_dofs: list[int] = Field(default_factory=list)
    displacement_values: dict[int, float] = Field(
        default_factory=dict,
        description="各 DOF 的指定位移（0.0=固定）",
    )


class Contact(BaseModel):
    """接触/连接约束（螺接接头等需要此定义）。"""
    contact_id: str
    contact_type: str = Field(description="'tie'（绑接）| 'contact'（接触）| 'coupling'（耦合）")
    master_set: str   # 主面集合名
    slave_set: str    # 从面集合名
    properties: dict[str, Any] = Field(default_factory=dict)


class AnalysisStep(BaseModel):
    """分析步定义：对应求解器中的一个 *STEP ... *END STEP 块。

    屈曲分析通常需要两步：预载静力步 + 屈曲特征值步。
    模态分析只需一个频率步。
    """
    step_id: str
    step_type: AnalysisStepType
    step_name: str
    nlgeom: bool = False         # 是否考虑几何非线性（大变形）
    inc_initial: float = 1.0    # 初始载荷增量
    inc_min: float = 1e-5       # 最小载荷增量（自动步长用）
    inc_max: float = 1.0        # 最大载荷增量
    time_period: float = 1.0    # 分析步时间长度（静力分析为伪时间）
    max_increments: int = 100   # 最大增量步数
    # 屈曲分析专用
    num_eigenmodes: int | None = None    # 提取的屈曲模态数
    # 模态分析专用
    num_frequencies: int | None = None  # 提取的固有频率数


class OutputRequestDef(BaseModel):
    """输出请求定义（传递给求解器的 *NODE FILE / *EL FILE 指令）。"""
    field_outputs: list[str] = Field(
        default_factory=lambda: ["U", "S", "E", "RF"],
        description="场输出变量（U=位移, S=应力, E=应变, RF=反力）",
    )
    history_outputs: list[str] = Field(
        default_factory=lambda: ["U", "RF"],
    )
    history_set_refs: list[str] = Field(default_factory=list)  # 历程输出的节点集
    frequency: int = Field(default=1, description="每隔 N 个增量输出一次")


class SolverExtensions(BaseModel):
    """求解器扩展层（Layer 3）：存储特定求解器的专属参数。

    例如：calculix["threads"] = 4 表示用 4 线程运行 CalculiX。
    这些参数不影响 Layer 2 的规范模型，仅在生成特定求解器输入时使用。
    """
    calculix: dict[str, Any] = Field(default_factory=dict)  # CalculiX 专属参数
    abaqus: dict[str, Any] = Field(default_factory=dict)    # Abaqus 专属参数
    nastran: dict[str, Any] = Field(default_factory=dict)   # Nastran 专属参数


# ---------------------------------------------------------------------------
# Root model  根模型
# ---------------------------------------------------------------------------

class AnalysisModel(BaseModel):
    """统一分析模型对象（analysis_model.json）。

    这是 CalculiX（或其他求解器）的求解描述，由 TemplateInstantiator 从
    CaseSpec + CaseTemplate 生成。

    与 CaseSpec 的区别：
        CaseSpec      → 用户填写，描述"要算什么问题"（问题定义语言）
        AnalysisModel → 机器生成，描述"怎么建立有限元模型"（有限元语言）

    三层结构：
        Layer 1（问题定义）：geometry_file, regions, loads, boundary_conditions
        Layer 2（规范模型）：materials, sections, analysis_steps, output_requests
        Layer 3（求解器扩展）：solver_extensions
    """

    metadata: Metadata = Field(default_factory=Metadata)
    template_linkage: TemplateLinkage = Field(default_factory=TemplateLinkage)

    # 几何文件引用（STEP 文件路径）
    geometry_file: str = Field(description="STEP 文件路径（相对于运行目录）")
    geometry_meta_file: str = Field(description="geometry_meta.json 路径")

    # Layer 1：几何区域和命名集合
    regions: list[Region] = Field(default_factory=list)
    sets: list[Set] = Field(default_factory=list)

    # Layer 2：材料和截面
    materials: list[CanonicalMaterial] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)

    # Layer 2：载荷和边界条件
    loads: list[LoadDef] = Field(default_factory=list)
    boundary_conditions: list[BoundaryConditionDef] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)  # 接触（螺接接头等）

    # Layer 2：分析步和输出请求
    analysis_steps: list[AnalysisStep] = Field(default_factory=list)
    output_requests: OutputRequestDef = Field(default_factory=OutputRequestDef)

    # Layer 3：求解器专属扩展
    solver_extensions: SolverExtensions = Field(default_factory=SolverExtensions)

    def to_json(self, indent: int = 2) -> str:
        """序列化为 JSON 字符串（写入 analysis_model.json）。"""
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "AnalysisModel":
        """从 analysis_model.json 反序列化（后处理阶段读取时使用）。"""
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
