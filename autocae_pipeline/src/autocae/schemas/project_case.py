"""Project Case Library schema (M2.1).

ProjectCase 是一个成功完成的 run 的沉淀记录，包含：
- 工程视图：面向设计师的可读信息（几何、材料、分析类型、载荷摘要）
- 计算视图：面向计算引擎的技术指标（网格统计、求解统计、结果摘要）

存储路径：project_case_library/cases/<case_id>.json
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ProjectCaseEngineeringView(BaseModel):
    """工程视图：面向设计师的可读信息。"""
    geometry_type: str
    topology: str
    analysis_type: str
    geometry_summary: str = ""      # 例如 "length=200mm, width=25mm, thickness=2mm"
    material_summary: str = ""      # 例如 "Al 7075-T6, E=71000MPa"
    load_summary: str = ""          # 例如 "tension 10000N at LOAD_END"
    bc_summary: str = ""            # 例如 "fixed at FIXED_END"
    features: list[str] = Field(default_factory=list)  # 特征列表


class ProjectCaseComputationView(BaseModel):
    """计算视图：面向计算引擎的技术指标。"""
    mesh_global_size: float | None = None
    mesh_element_type: str | None = None
    mesh_node_count: int | None = None
    mesh_element_count: int | None = None
    mesh_min_quality: float | None = None
    mesh_overall_pass: bool | None = None
    solver: str = "calculix"
    dry_run: bool = False
    wall_time_s: float | None = None
    max_displacement: float | None = None
    max_mises_stress: float | None = None
    buckling_load_factor: float | None = None
    natural_frequencies: list[float] = Field(default_factory=list)


class ProjectCase(BaseModel):
    """项目案例记录（M2.1 数据模型）。

    一个 ProjectCase 对应一次成功的流水线 run，支持：
    1. 工程视图 + 计算视图（双视图）
    2. 从 Project Case 反查对应 run 与产物
    3. 参与 intake 检索与推荐（M2.3 affinity 字段）
    """
    case_id: str = Field(default_factory=lambda: f"pc_{uuid.uuid4().hex[:8]}")
    source_case_id: str                         # 原始 CaseSpec 的 case_id
    case_name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    run_dir: str                                 # 对应的 runs/<case_id>/ 路径
    source_spec_path: str                        # case_spec.json 绝对路径
    template_id: str | None = None              # 若来自模板，记录模板 ID
    template_affinity: float | None = None      # M2.3: 来自 CaseSpec 元数据
    template_link: str | None = None            # M2.3: 来源模板链接
    status: str = "completed"                   # "completed" | "failed"
    tags: list[str] = Field(default_factory=list)
    engineering_view: ProjectCaseEngineeringView
    computation_view: ProjectCaseComputationView
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "ProjectCase":
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
