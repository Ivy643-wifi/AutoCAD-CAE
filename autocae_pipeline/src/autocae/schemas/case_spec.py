"""CaseSpec data schema.

case_spec.json — 标准算例定义对象（流水线最核心的输入数据）。
生产者：CaseSpec 模块（用户填写 YAML 后由 Builder 解析）。
消费者：模板匹配、CAD 建模、网格生成、分析模型构建等后续所有模块。

设计原则：CaseSpec 只描述"问题是什么"，而不是"怎么求解"。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations  枚举类：定义所有允许的关键字
# ---------------------------------------------------------------------------

class Topology(str, Enum):
    """结构拓扑类型（层级第一级）：描述零件的大类。"""
    LAMINATE = "laminate"    # 层合板（多层复合材料铺层）
    SHELL = "shell"          # 薄壳结构（圆柱壳等）
    BEAM = "beam"            # 梁结构
    PANEL = "panel"          # 加筋板（带长桁的蒙皮板）
    SANDWICH = "sandwich"    # 夹芯板（面板+芯材）
    JOINT = "joint"          # 连接件（螺接、铆接等）


class GeometryType(str, Enum):
    """结构族标识符（层级第二级）：在大类下进一步细化几何形状。"""
    FLAT_PLATE = "flat_plate"                          # 矩形平板
    OPEN_HOLE_PLATE = "open_hole_plate"                # 开孔平板（OHT/OHC 试验件）
    NOTCHED_PLATE = "notched_plate"                    # 缺口平板
    CYLINDRICAL_SHELL = "cylindrical_shell"            # 圆柱壳
    PRESSURE_SHELL = "pressure_shell"                  # 压力容器壳
    LAMINATED_BEAM = "laminated_beam"                  # 层合梁
    STRINGER_STIFFENED_PANEL = "stringer_stiffened_panel"  # 长桁加筋壁板
    SANDWICH_PLATE = "sandwich_plate"                  # 夹芯板
    BOLTED_LAP_JOINT = "bolted_lap_joint"              # 螺接搭接接头


class AnalysisType(str, Enum):
    """支持的分析类型（层级第三级）：规定对该结构做什么计算。"""
    STATIC_TENSION = "static_tension"         # 静力拉伸
    STATIC_COMPRESSION = "static_compression" # 静力压缩
    BENDING = "bending"                       # 弯曲
    BUCKLING = "buckling"                     # 屈曲（线性特征值屈曲）
    MODAL = "modal"                           # 模态（自由振动频率）
    IMPACT = "impact"                         # 冲击（动力学）
    FATIGUE = "fatigue"                       # 疲劳
    THERMAL = "thermal"                       # 热分析
    SHEAR = "shear"                           # 剪切
    TORSION = "torsion"                       # 扭转
    PRESSURE = "pressure"                     # 内/外压


class LoadType(str, Enum):
    """载荷类型枚举：描述施加在结构上的载荷种类。"""
    TENSION = "tension"           # 拉伸载荷
    COMPRESSION = "compression"   # 压缩载荷
    BENDING = "bending"           # 弯曲载荷
    SHEAR = "shear"               # 剪切载荷
    TORSION = "torsion"           # 扭矩载荷
    PRESSURE = "pressure"         # 均布压强
    POINT_FORCE = "point_force"   # 集中力
    MOMENT = "moment"             # 集中力矩
    THERMAL = "thermal"           # 热载荷（温度场）


class BoundaryType(str, Enum):
    """边界条件类型枚举：描述约束的程度。"""
    FIXED = "fixed"                        # 固支（所有自由度=0）
    PINNED = "pinned"                      # 铰支（平动自由度=0，转动自由）
    SIMPLY_SUPPORTED = "simply_supported"  # 简支（仅法向位移=0）
    SYMMETRY = "symmetry"                  # 对称面约束
    FREE = "free"                          # 自由端（不约束）
    ENCASTRE = "encastre"                  # Abaqus 风格全固支


class FeatureName(str, Enum):
    """结构特征（几何细节）枚举：附加在基础几何体上的特征。"""
    HOLE = "hole"                              # 孔
    CUTOUT = "cutout"                          # 开口/缺口
    STIFFENER = "stiffener"                    # 加强筋
    CORE = "core"                              # 夹芯（用于夹芯板）
    FASTENER = "fastener"                      # 紧固件（螺栓/铆钉）
    REPAIR = "repair"                          # 修理区域
    CURVATURE = "curvature"                    # 曲率（使平板变成曲面板）
    THICKNESS_CHANGE = "thickness_change"      # 厚度阶差
    LAYUP = "layup"                            # 铺层信息（覆盖默认铺层）
    MANUFACTURING_TAGS = "manufacturing_tags"  # 制造标注


class ElementType(str, Enum):
    """网格单元类型偏好（提示 MeshBuilder 生成什么类型的单元）。"""
    S4R = "S4R"     # 4 节点减积分壳单元（适合薄壳）
    S8R = "S8R"     # 8 节点减积分壳单元（二阶壳）
    C3D8R = "C3D8R" # 8 节点减积分六面体实体单元
    C3D10 = "C3D10" # 10 节点四面体单元（适合复杂几何）
    B31 = "B31"     # 2 节点 Timoshenko 梁单元
    B32 = "B32"     # 3 节点 Timoshenko 梁单元（二阶）
    AUTO = "auto"   # 由 MeshBuilder 自动选择


# ---------------------------------------------------------------------------
# Sub-models  子模型：CaseSpec 的各字段类型定义
# ---------------------------------------------------------------------------

class Geometry(BaseModel):
    """几何参数：描述结构族的尺寸（单位：mm）。"""
    geometry_type: GeometryType
    length: float = Field(gt=0, description="主长度方向（加载方向，X 轴）[mm]")
    width: float = Field(gt=0, description="宽度方向（Y 轴）[mm]")
    thickness: float = Field(gt=0, description="厚度（Z 轴）[mm]")
    # extra 用于存储特定结构族的额外参数，如开孔板的 hole_diameter、圆柱壳的 radius 等
    extra: dict[str, float] = Field(
        default_factory=dict,
        description="附加几何参数（如 hole_diameter、radius），均须为正数",
    )

    @field_validator("extra")
    @classmethod
    def _extra_values_positive(cls, v: dict[str, float]) -> dict[str, float]:
        # 校验规则：extra 字段中所有值必须为正数
        for key, val in v.items():
            if val <= 0:
                raise ValueError(f"Geometry extra field '{key}' must be positive, got {val}")
        return v


class LayupLayer(BaseModel):
    """单层铺层定义：层合板中的一层纤维铺层。

    铺层角 angle：纤维方向与参考方向（X 轴）的夹角，单位度。
    常见铺层序列如 [0/45/-45/90]s（对称铺层）。
    """
    angle: float = Field(description="纤维取向角 [deg]，相对于 X 轴正方向")
    thickness: float = Field(gt=0, description="单层厚度 [mm]")
    material_id: str = Field(default="default", description="引用 materials 列表中的材料 ID")


class Material(BaseModel):
    """材料定义：支持各向同性（金属）和正交各向异性（复合材料）。

    各向同性：只需给 E（杨氏模量）和 nu（泊松比）。
    正交各向异性：需给 E1, E2, G12, nu12（纤维方向与横向的弹性常数）。
    至少满足其中一组，否则 Pydantic 校验报错。
    """
    material_id: str = Field(default_factory=lambda: f"mat_{uuid.uuid4().hex[:6]}")
    name: str

    # 各向同性弹性参数
    E: float | None = Field(default=None, gt=0, description="杨氏模量 [MPa]（各向同性）")
    nu: float | None = Field(default=None, ge=0, lt=0.5, description="泊松比（须小于 0.5）")
    rho: float | None = Field(default=None, gt=0, description="密度 [kg/mm³]")

    # 正交各向异性参数（复合材料）
    E1: float | None = Field(default=None, gt=0, description="纤维方向杨氏模量 [MPa]")
    E2: float | None = Field(default=None, gt=0, description="横向杨氏模量 [MPa]")
    G12: float | None = Field(default=None, gt=0, description="面内剪切模量 [MPa]")
    nu12: float | None = Field(default=None, ge=0, lt=1.0, description="主泊松比")

    # 强度值（可选，用于后处理失效判断）
    Xt: float | None = Field(default=None, gt=0, description="纤维方向拉伸强度 [MPa]")
    Xc: float | None = Field(default=None, gt=0, description="纤维方向压缩强度 [MPa]")

    @model_validator(mode="after")
    def _check_property_completeness(self) -> "Material":
        # 校验规则：必须提供各向同性或正交各向异性中的至少一组参数
        has_iso = self.E is not None and self.nu is not None
        has_ortho = self.E1 is not None and self.E2 is not None and self.G12 is not None
        if not has_iso and not has_ortho:
            raise ValueError(
                f"Material '{self.name}' must define either isotropic (E, nu) "
                "or orthotropic (E1, E2, G12) properties."
            )
        return self


class Feature(BaseModel):
    """结构特征：附加在基础几何上的几何细节（如孔、加筋等）。

    enabled=True 时该特征才会被 CAD 模板处理。
    params 存放特征相关参数，例如孔的直径：{"diameter": 6.0}。
    """
    name: FeatureName
    enabled: bool = True
    params: dict[str, float] = Field(default_factory=dict)


class Load(BaseModel):
    """载荷定义：施加在命名区域上的载荷。

    location 对应 CAD 模板中的命名面/边，例如 "LOAD_END"（加载端面）。
    direction 是三维单位向量，用于确定力/位移的方向。
    """
    load_id: str = Field(default_factory=lambda: f"load_{uuid.uuid4().hex[:6]}")
    load_type: LoadType
    magnitude: float = Field(description="载荷大小（N、MPa 等，取决于载荷类型）")
    location: str = Field(
        default="end",
        description="载荷施加区域的命名（须与 CAD 模板中的命名面对应）",
    )
    direction: list[float] = Field(
        default_factory=lambda: [1.0, 0.0, 0.0],
        description="载荷方向单位向量 [dx, dy, dz]",
    )

    @field_validator("direction")
    @classmethod
    def _direction_length(cls, v: list[float]) -> list[float]:
        # 校验规则：方向向量必须是三分量的
        if len(v) != 3:
            raise ValueError("direction must be a 3-component vector")
        return v


class BoundaryCondition(BaseModel):
    """边界条件：约束指定命名区域的自由度（DOF）。

    constrained_dofs 列表：1=Ux, 2=Uy, 3=Uz, 4=Rx, 5=Ry, 6=Rz
    空列表意味着所有 DOF 均被约束（完全固支）。
    """
    bc_id: str = Field(default_factory=lambda: f"bc_{uuid.uuid4().hex[:6]}")
    bc_type: BoundaryType
    location: str = Field(description="约束施加区域的命名（如 'FIXED_END'）")
    constrained_dofs: list[int] = Field(
        default_factory=list,
        description="被约束的自由度编号（1-6），空表示全约束",
    )


class MeshPreferences(BaseModel):
    """网格生成偏好：传递给 MeshBuilder（Gmsh）的配置参数。"""
    global_size: float = Field(gt=0, description="全局目标单元尺寸 [mm]")
    local_refinements: dict[str, float] = Field(
        default_factory=dict,
        description="局部加密：命名区域 → 局部单元尺寸（如应力集中区加密）",
    )
    element_type: ElementType = ElementType.AUTO
    min_quality: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="最低可接受的 Jacobian 单元质量（0~1，0.3 以下通常不可接受）",
    )
    optimize_passes: int = Field(default=3, ge=0, le=10,
                                  description="Netgen 优化迭代次数")


class OutputRequest(BaseModel):
    """后处理输出请求：告诉求解器和后处理引擎要提取哪些结果变量。

    field_outputs: 场输出（整个模型的空间分布场）
      U=位移, S=应力, E=应变, RF=反力
    history_outputs: 历程输出（选定节点/单元随载荷步的变化曲线）
    """
    field_outputs: list[str] = Field(
        default_factory=lambda: ["U", "S", "E", "RF"],
        description="场输出变量（U=位移, S=应力, E=应变, RF=反力）",
    )
    history_outputs: list[str] = Field(
        default_factory=lambda: ["U", "RF"],
        description="历程输出变量（生成载荷-位移曲线等）",
    )
    generate_plots: bool = True    # 是否自动生成结果云图/曲线图
    generate_report: bool = False  # 是否生成 PDF 报告（未来功能）


class CaseSpecMetadata(BaseModel):
    """算例元信息：用于管理和追踪算例。"""
    case_id: str = Field(default_factory=lambda: f"case_{uuid.uuid4().hex[:8]}")
    case_name: str                               # 算例名称（人类可读）
    version: str = "1.0"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    source: str = "user"                         # 来源：user / template / import
    status: str = "draft"                        # 状态：draft / validated / approved
    template_id: str | None = None               # 若来自模板，记录模板 ID
    notes: str = ""
    # M2.3: 检索亲和度与来源模板链接
    template_affinity: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="检索匹配相似度（0~1，由 IntakeService/TemplateRegistry 写入，参与检索排序）",
    )
    template_link: str | None = Field(
        default=None,
        description="来源模板 ID 或路径（用于追溯、推荐和 promote 流程）",
    )


# ---------------------------------------------------------------------------
# Root model  根模型：流水线最核心的输入对象
# ---------------------------------------------------------------------------

class CaseSpec(BaseModel):
    """标准算例定义对象（case_spec.json）。

    这是整个 AutoCAE 流水线的起点，也是唯一的"问题输入"。

    层级结构：
        Topology（大类）→ GeometryType（结构族）→ AnalysisType（分析工况）

    注意：
        - CaseSpec 描述"要算什么"，不涉及"如何求解"（那是 AnalysisModel 的职责）。
        - LAMINATE 拓扑必须提供至少一层铺层（layup 不能为空）。
        - materials 和 loads、boundary_conditions 各至少一项，Pydantic 会校验。
    """

    metadata: CaseSpecMetadata
    topology: Topology                   # 结构拓扑大类
    geometry: Geometry                   # 几何参数（尺寸）
    layup: list[LayupLayer] = Field(
        default_factory=list,
        description="铺层序列（层合板必填；各向同性结构留空）",
    )
    materials: Annotated[list[Material], Field(min_length=1)]  # 至少一种材料
    features: list[Feature] = Field(default_factory=list)      # 特征列表（可选）
    loads: Annotated[list[Load], Field(min_length=1)]           # 至少一个载荷
    boundary_conditions: Annotated[list[BoundaryCondition], Field(min_length=1)]  # 至少一个边界条件
    analysis_type: AnalysisType          # 分析类型
    mesh_preferences: MeshPreferences   # 网格偏好
    output_requests: OutputRequest = Field(default_factory=OutputRequest)  # 输出请求
    template_preferences: dict[str, Any] = Field(
        default_factory=dict,
        description="可选：强制指定 template_id 或提供匹配提示",
    )

    @model_validator(mode="after")
    def _check_layup_for_laminate(self) -> "CaseSpec":
        # 业务规则：层合板（LAMINATE）拓扑必须有铺层定义
        if self.topology == Topology.LAMINATE and not self.layup:
            raise ValueError(
                "A LAMINATE topology requires at least one layer in 'layup'."
            )
        return self

    def to_json(self, indent: int = 2) -> str:
        """序列化为 JSON 字符串（用于保存 case_spec.json）。"""
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_yaml(cls, path: str) -> "CaseSpec":
        """从 YAML 文件加载 CaseSpec（用户最常用的输入方式）。"""
        import yaml
        from pathlib import Path
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    @classmethod
    def from_json(cls, path: str) -> "CaseSpec":
        """从 JSON 文件加载 CaseSpec（流水线内部传递时使用）。"""
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
