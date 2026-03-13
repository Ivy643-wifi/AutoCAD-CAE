"""Mesh-related data schemas.

geometry_meta.json      – CAD Builder 生成的几何元信息（STEP 文件的摘要描述）。
mesh_groups.json        – Mesh Builder 生成的网格分组映射（Gmsh Physical Group → 求解器集合名）。
mesh_quality_report.json – Mesh Builder 生成的网格质量报告。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class GeometrySource(str, Enum):
    """几何来源：标记 STEP 文件是由哪个工具生成的。"""
    CADQUERY = "cadquery"          # 由 CadQuery 参数化建模生成
    EXTERNAL_STEP = "external_step"  # 用户直接提供的 STEP 文件
    FREECAD = "freecad"            # 由 FreeCAD 生成


class GeometryMeta(BaseModel):
    """geometry_meta.json — CAD Builder 输出的几何元信息。

    作用：作为 CAD 阶段到 Mesh 阶段的信息传递桥梁。
    包含：
        - STEP 文件路径
        - 命名拓扑实体（面/边/点的编号映射）
            这些名称由 CAD 模板预先定义（如 FIXED_END、LOAD_END），
            Gmsh 导入 STEP 后会用 bounding box 位置启发式方法进行匹配。
        - 包围盒（bounding_box）：用于 MeshBuilder 的位置启发式分组
        - 体积/面积统计
    """

    geometry_id: str = Field(default_factory=lambda: f"geo_{uuid.uuid4().hex[:8]}")
    step_file: str = Field(description="STEP 文件路径")
    source: GeometrySource         # 几何来源（CadQuery / 外部 STEP / FreeCAD）
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # 命名拓扑实体（由 Gmsh 导入 STEP 后填充）
    # 键：工程含义名称（如 'FIXED_END'）；值：Gmsh surface/curve/point tag 列表
    named_faces: dict[str, list[int]] = Field(
        default_factory=dict,
        description="命名面 → Gmsh surface tag 列表（MeshBuilder 填充）",
    )
    named_edges: dict[str, list[int]] = Field(
        default_factory=dict,
        description="命名边 → Gmsh curve tag 列表",
    )
    named_vertices: dict[str, list[int]] = Field(
        default_factory=dict,
        description="命名点 → Gmsh point tag 列表",
    )

    # 包围盒：用于位置启发式（面的质心与 xmin/xmax 对比）
    bounding_box: dict[str, float] = Field(
        default_factory=dict,
        description="模型包围盒：xmin, xmax, ymin, ymax, zmin, zmax [mm]",
    )
    volume: float | None = None   # 模型总体积 [mm³]
    area: float | None = None     # 模型总表面积 [mm²]
    import_warnings: list[str] = Field(default_factory=list)  # STEP 导入时的警告
    repair_applied: bool = False  # 是否对 STEP 几何做了修复

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "GeometryMeta":
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class MeshGroup(BaseModel):
    """单个网格分组（对应 Gmsh 中的一个 Physical Group）。

    Gmsh Physical Group 是网格中命名的节点/单元集合，
    对应 CalculiX 输入文件中的 *NSET 或 *ELSET 名称。

    映射关系：Gmsh Physical Group → MeshGroup → 求解器集合名
    例如：Physical Group "FIXED_END"（tag=1, 包含面 1,2）
          → solver_set_name="FIXED_END"（用于 *BOUNDARY, FIXED_END, 1,6,0.0）
    """
    group_id: str
    entity_type: str = Field(description="实体维度：'volume'|'surface'|'curve'|'point'")
    gmsh_tag: int = Field(description="Gmsh Physical Group 的整数 tag")
    mapped_region: str = Field(description="对应 GeometryMeta 中的命名实体名")
    solver_set_name: str = Field(description="求解器输入文件中的集合名（大写）")
    gmsh_entity_tags: list[int] = Field(
        default_factory=list,
        description="该 Physical Group 包含的 Gmsh 底层实体 tag 列表",
    )


class MeshGroups(BaseModel):
    """mesh_groups.json — 完整的网格分组映射。

    由 MeshBuilder 生成，SolverAdapter 读取以确定在 job.inp 中如何引用各集合。
    """

    geometry_id: str                  # 对应的 GeometryMeta.geometry_id
    mesh_file: str = Field(description="网格文件路径（Gmsh 导出的 .inp 文件）")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    groups: list[MeshGroup] = Field(default_factory=list)  # 所有 Physical Group
    node_count: int = 0     # 总节点数
    element_count: int = 0  # 总单元数

    def get_group(self, solver_set_name: str) -> MeshGroup | None:
        """按求解器集合名查找 MeshGroup（忽略大小写差异不处理，调用方负责大写）。"""
        for g in self.groups:
            if g.solver_set_name == solver_set_name:
                return g
        return None

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "MeshGroups":
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class QualityCheck(BaseModel):
    """单项网格质量检查的结果。"""
    check_name: str           # 检查项名称（如 'min_jacobian_quality'）
    passed: bool              # 是否通过
    value: float | None = None     # 实际值
    threshold: float | None = None # 阈值
    message: str = ""         # 未通过时的描述信息


class MeshQualityReport(BaseModel):
    """mesh_quality_report.json — 网格质量报告。

    关键指标：
        min_quality：最差单元的 Jacobian 质量（0~1，<0.3 通常不可接受）
        avg_quality：平均 Jacobian 质量
        max_aspect_ratio：最大长宽比（高值说明有扁平单元）
        overall_pass：是否通过所有质量检查（False 时流水线警告但不中止）
    """

    geometry_id: str
    mesh_file: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    element_count: int           # 总单元数
    node_count: int              # 总节点数
    min_quality: float = Field(description="最差单元 Jacobian 质量（0~1）")
    avg_quality: float           # 平均质量
    max_aspect_ratio: float      # 最大长宽比
    checks: list[QualityCheck] = Field(default_factory=list)  # 逐项检查结果
    warnings: list[str] = Field(default_factory=list)         # 质量警告信息
    failed_checks: list[str] = Field(default_factory=list)    # 未通过的检查项名称
    overall_pass: bool = True    # 总体是否通过
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "MeshQualityReport":
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
