"""Post-processing and review data schemas.

result_summary.json        – 关键标量结果（最大位移、最大 Mises 应力、屈曲载荷因子等）。
field_manifest.json        – 场结果目录（各场量对应的 VTK 文件路径）。
history_data.csv           – 载荷-位移等时程曲线数据。
diagnostics.json           – 收敛情况、警告、可信度评级。
review_report.json         – 审核门禁决策输出（通过/拒绝/有条件）。
library_update_request.json – 写回长期库的请求（仅审核通过后触发）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ReviewStatus(str, Enum):
    """审核状态（设计原则 G-09：只有审核通过的算例才写回库）。"""
    APPROVED = "approved"        # 通过（可写回库）
    REJECTED = "rejected"        # 拒绝（结果不可信）
    PENDING = "pending"          # 待审核
    CONDITIONAL = "conditional"  # 有条件通过（需人工复核）


class LibraryTarget(str, Enum):
    """写回目标库：结果可以归档到哪种库。"""
    CASE_SPEC_LIBRARY = "case_spec_library"    # 算例规格库
    TEMPLATE_LIBRARY = "template_library"      # 模板库
    KNOWLEDGE_BASE = "knowledge_base"          # 知识库


class LibraryAction(str, Enum):
    """库操作类型。"""
    ADD = "add"            # 新增条目
    UPDATE = "update"      # 更新已有条目
    DEPRECATE = "deprecate"  # 标记为废弃


# ---------------------------------------------------------------------------
# Result summary  结果摘要
# ---------------------------------------------------------------------------

class ScalarResult(BaseModel):
    """单个标量结果条目（用于 extra_scalars 扩展字段）。"""
    name: str           # 结果名称（如 'max_shear_stress'）
    value: float        # 数值
    unit: str = ""      # 单位（如 'MPa', 'mm'）
    location: str = ""  # 位置描述（节点/单元 ID 或集合名）


class ResultSummary(BaseModel):
    """result_summary.json — 关键标量结果摘要。

    这是流水线最终交付的核心结果，包含：
        位移：max_displacement（最大合位移 [mm]）及其节点 ID
        应力：max_mises_stress（最大 von Mises 等效应力 [MPa]）
        反力：total_reaction_force（固定端反力合力 [N]）
        屈曲：buckling_load_factor（线性屈曲载荷因子，BLF<1 表示已屈曲）
        模态：natural_frequencies（前 N 阶固有频率 [Hz]）
    """

    job_id: str
    analysis_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # 位移结果
    max_displacement: float | None = None         # 最大合位移 [mm]
    max_displacement_node: int | None = None      # 发生最大位移的节点 ID

    # 应力结果
    max_mises_stress: float | None = None         # 最大 von Mises 应力 [MPa]
    max_mises_element: int | None = None          # 发生最大应力的节点/单元 ID
    max_principal_stress: float | None = None     # 最大主应力 [MPa]
    min_principal_stress: float | None = None     # 最小主应力（压缩为负）[MPa]

    # 反力
    total_reaction_force: list[float] | None = None  # 合力 [Fx, Fy, Fz] [N]

    # 屈曲特征值
    buckling_load_factor: float | None = None     # 线性屈曲载荷因子（BLF）

    # 模态频率
    natural_frequencies: list[float] | None = None  # 前 N 阶固有频率 [Hz]

    # 载荷-位移曲线（每个点 [total_load_N, max_disp_mm]，按步号排序）
    load_displacement_curve: list[list[float]] | None = None

    # 额外自定义标量
    extra_scalars: list[ScalarResult] = Field(default_factory=list)

    analysis_type: str = ""   # 分析类型（从 AnalysisStep 读取）
    units: dict[str, str] = Field(
        default_factory=lambda: {
            "displacement": "mm",
            "stress": "MPa",
            "force": "N",
            "moment": "N·mm",
        }
    )

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "ResultSummary":
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Field manifest  场结果目录
# ---------------------------------------------------------------------------

class FieldResult(BaseModel):
    """场结果目录中的一条记录：描述某一步骤某一场量的 VTK 文件位置。"""
    field_name: str = Field(description="场量名称，如 'U'（位移）, 'S'（应力）")
    step: str               # 分析步标识
    frame: int              # 增量帧号
    region: str = "whole_model"  # 区域（整个模型或某命名集合）
    storage_path: str = Field(description="VTK/CSV 文件的路径")
    components: list[str] = Field(
        default_factory=list,
        description="场量分量列表（如 ['U1', 'U2', 'U3', 'Umag']）",
    )
    element_count: int | None = None  # 该场的单元/节点数


class FieldManifest(BaseModel):
    """field_manifest.json — 场结果目录。

    记录所有可用场量的存储位置，供可视化工具（PyVista/ParaView）加载。
    注意：本文件只是"目录"，不包含场量数据本身（数据在 VTK 文件中）。
    """

    job_id: str
    analysis_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    fields: list[FieldResult] = Field(default_factory=list)  # 所有场量记录

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "FieldManifest":
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Diagnostics  诊断报告
# ---------------------------------------------------------------------------

class DiagnosticCheck(BaseModel):
    """单项诊断检查结果。

    layer 字段标识诊断属于哪个层次：
        'input'     → Layer A：输入校验（CaseSpec 业务规则）
        'interface' → Layer B：接口校验（文件存在、格式正确、物理组完整）
        'runtime'   → Layer C：运行时诊断（求解器收敛、日志解析）
        'repair'    → Layer D：修复建议（如何修复失败的检查）
    """
    layer: str = Field(description="诊断层次：'input'|'interface'|'runtime'|'repair'")
    check_name: str    # 检查项标识（如 'step_file_exists', 'ccx_convergence'）
    passed: bool       # 是否通过
    message: str = ""  # 未通过时的描述
    suggestion: str = ""  # 修复建议（Layer D）


class Diagnostics(BaseModel):
    """diagnostics.json — 全流水线诊断报告。

    汇总所有四层诊断检查的结果，并给出可信度评级（trust_level）。

    trust_level：
        1.0 → 完全可信（求解收敛，所有检查通过）
        0.0 → 不可信（求解未完成或有致命错误）
        0~1 → 部分可信（有警告但未致命）
    """

    job_id: str
    analysis_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    checks: list[DiagnosticCheck] = Field(default_factory=list)  # 所有诊断检查记录
    warnings: list[str] = Field(default_factory=list)            # 警告信息
    errors: list[str] = Field(default_factory=list)              # 错误信息
    convergence_achieved: bool | None = None   # 求解器是否收敛
    convergence_iterations: int | None = None  # 收敛迭代次数
    trust_level: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="结果可信度（0=不可信，1=完全可信）",
    )
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "Diagnostics":
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Review Gate  审核门禁
# ---------------------------------------------------------------------------

class ReviewReport(BaseModel):
    """review_report.json — 审核门禁决策输出（设计原则 G-09）。

    只有 review_status=APPROVED 的算例才会生成 LibraryUpdateRequest，
    触发写回长期模板库或知识库。
    """

    review_id: str = Field(default_factory=lambda: f"rev_{uuid.uuid4().hex[:8]}")
    job_id: str
    analysis_id: str
    review_status: ReviewStatus = ReviewStatus.PENDING
    reviewer_rules: list[str] = Field(default_factory=list)  # 触发审核的规则列表
    reasons: list[str] = Field(default_factory=list)         # 审核理由
    library_recommendation: LibraryTarget | None = None      # 建议归档的目标库
    reviewed_at: datetime = Field(default_factory=datetime.utcnow)
    notes: str = ""

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "ReviewReport":
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Library Update  写回库请求
# ---------------------------------------------------------------------------

class LibraryUpdateRequest(BaseModel):
    """library_update_request.json — 请求将算例/模板写回长期库。

    只有审核通过（ReviewStatus.APPROVED）后才会创建此对象（设计原则 G-09）。
    """

    request_id: str = Field(default_factory=lambda: f"lib_{uuid.uuid4().hex[:8]}")
    review_id: str              # 对应 ReviewReport.review_id
    target_library: LibraryTarget
    action: LibraryAction
    object_type: str = Field(description="写回对象类型：'case_spec'|'template'|'result'|'knowledge'")
    object_ref: str = Field(description="写回对象的 ID")
    versioning: dict[str, str] = Field(
        default_factory=dict,
        description="版本元信息（如 {'version': '1.1', 'tag': 'validated'}）",
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "LibraryUpdateRequest":
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
