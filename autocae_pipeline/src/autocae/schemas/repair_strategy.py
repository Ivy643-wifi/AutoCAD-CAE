"""Shared auto-repair strategy model for CAD and Mesh LLM stages (M2.4).

统一 CAD/Mesh 两阶段的修复策略数据模型和错误分类体系，
确保修复日志结构一致、issue_report 可直接消费修复历史。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Error taxonomy  错误分类体系
# ---------------------------------------------------------------------------

class ErrorClass(str, Enum):
    """标准化错误分类（CAD/Mesh 阶段共用）。"""
    SYNTAX_ERROR           = "syntax_error"           # Python 语法错误
    IMPORT_ERROR           = "import_error"            # 缺少依赖模块
    RUNTIME_ERROR          = "runtime_error"           # 运行时异常
    EXPORT_MISSING         = "export_missing"          # 脚本未输出必要文件
    FILE_NOT_FOUND         = "file_not_found"          # 文件路径错误
    GEOMETRIC_INVALID      = "geometric_invalid"       # 几何体无效（CAD 专用）
    QUALITY_BELOW_THRESHOLD = "quality_below_threshold" # 网格质量不达标（Mesh 专用）
    UNKNOWN                = "unknown"                 # 无法归类的错误


# ---------------------------------------------------------------------------
# Shared repair config  共享修复配置
# ---------------------------------------------------------------------------

@dataclass
class RepairConfig:
    """有界重试控制（CAD/Mesh 阶段共享配置模型）。

    完成标准（M1.4/M1.5）：
        - max_attempts: 最大重试次数上限
        - failure_class_filter: 允许触发修复的错误类别
        - stop_conditions: 终止修复循环的条件
        - repeated_failure_limit: 同类错误连续出现上限
    """
    max_attempts: int = 3
    failure_class_filter: tuple[str, ...] = (
        ErrorClass.SYNTAX_ERROR,
        ErrorClass.IMPORT_ERROR,
        ErrorClass.RUNTIME_ERROR,
        ErrorClass.EXPORT_MISSING,
    )
    stop_conditions: tuple[str, ...] = (
        "success",
        "max_attempts_reached",
        "failure_class_not_allowed",
        "repeated_failure_limit",
    )
    repeated_failure_limit: int = 2


# ---------------------------------------------------------------------------
# Repair attempt log  单次修复迭代记录
# ---------------------------------------------------------------------------

@dataclass
class RepairAttempt:
    """单次修复迭代的结构化日志条目（M2.4 统一格式）。"""
    attempt: int
    stage: str                         # "cad_llm" | "mesh_llm"
    script_path: str
    execution_log_path: str
    error_class: str
    error_message: str
    repair_action: str                 # "initial_generation" | "repair_from_<error>"
    round_result: str                  # "success" | "failed"
    provider_meta: dict[str, Any] = field(default_factory=dict)
    input_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Repair audit log  完整修复会话审计记录
# ---------------------------------------------------------------------------

@dataclass
class RepairAuditLog:
    """完整修复会话的审计日志（可直接序列化为 JSON）。"""
    stage: str                         # "cad_llm" | "mesh_llm"
    status: str                        # "success" | "failed"
    stop_reason: str
    started_at_utc: str
    ended_at_utc: str
    config: dict[str, Any]
    input_summary: dict[str, Any]
    attempts: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Shared hint functions  共享的根因 / 修复建议生成函数
# ---------------------------------------------------------------------------

_ROOT_CAUSE_HINTS: dict[str, dict[str, str]] = {
    "cad_llm": {
        ErrorClass.SYNTAX_ERROR:            "Generated CAD script has Python syntax issues.",
        ErrorClass.IMPORT_ERROR:            "Runtime environment is missing required Python modules (cadquery).",
        ErrorClass.EXPORT_MISSING:          "CAD script did not write required artifacts (model.step, geometry_meta.json).",
        ErrorClass.FILE_NOT_FOUND:          "Expected file path in CAD script is invalid.",
        ErrorClass.RUNTIME_ERROR:           "CAD script execution failed with runtime exception.",
        ErrorClass.GEOMETRIC_INVALID:       "Generated geometry is invalid or degenerate.",
        ErrorClass.QUALITY_BELOW_THRESHOLD: "N/A for CAD stage.",
        ErrorClass.UNKNOWN:                 "Unknown CAD script failure.",
    },
    "mesh_llm": {
        ErrorClass.SYNTAX_ERROR:            "Generated mesh script has Python syntax issues.",
        ErrorClass.IMPORT_ERROR:            "Runtime environment is missing required Python modules (gmsh).",
        ErrorClass.EXPORT_MISSING:          "Mesh script did not write required artifacts (mesh.inp, mesh_groups.json, mesh_quality_report.json).",
        ErrorClass.FILE_NOT_FOUND:          "Expected file path in mesh script is invalid.",
        ErrorClass.RUNTIME_ERROR:           "Mesh script execution failed with runtime exception.",
        ErrorClass.GEOMETRIC_INVALID:       "N/A for mesh stage.",
        ErrorClass.QUALITY_BELOW_THRESHOLD: "Generated mesh quality is below the minimum acceptable threshold.",
        ErrorClass.UNKNOWN:                 "Unknown mesh script failure.",
    },
}

_REMEDIATION_HINTS: dict[str, dict[str, str]] = {
    "cad_llm": {
        ErrorClass.SYNTAX_ERROR:   "Fix script syntax and re-run with bounded retry.",
        ErrorClass.IMPORT_ERROR:   "Install missing dependency (cadquery) and re-run.",
        ErrorClass.EXPORT_MISSING: "Ensure script writes model.step and geometry_meta.json.",
        ErrorClass.FILE_NOT_FOUND: "Check output path handling and file permissions.",
        ErrorClass.RUNTIME_ERROR:  "Inspect execution log, then regenerate script with error context.",
        ErrorClass.GEOMETRIC_INVALID: "Adjust geometry parameters or use a fallback template.",
        ErrorClass.UNKNOWN:        "Inspect cad_llm_repair_audit.json for details.",
    },
    "mesh_llm": {
        ErrorClass.SYNTAX_ERROR:   "Fix script syntax and re-run with bounded retry.",
        ErrorClass.IMPORT_ERROR:   "Install missing dependency (gmsh) and re-run.",
        ErrorClass.EXPORT_MISSING: "Ensure script writes mesh.inp, mesh_groups.json, mesh_quality_report.json.",
        ErrorClass.FILE_NOT_FOUND: "Check output path handling and file permissions.",
        ErrorClass.RUNTIME_ERROR:  "Inspect execution log, then regenerate script with error context.",
        ErrorClass.QUALITY_BELOW_THRESHOLD: "Reduce global_size or add local refinements to improve mesh quality.",
        ErrorClass.UNKNOWN:        "Inspect mesh_llm_repair_audit.json for details.",
    },
}


def root_cause_hint(error_class: str, stage: str = "cad_llm") -> str:
    """根因提示（统一函数，CAD/Mesh 通用）。"""
    stage_map = _ROOT_CAUSE_HINTS.get(stage, _ROOT_CAUSE_HINTS["cad_llm"])
    return stage_map.get(error_class, f"Unknown failure in {stage} stage.")


def remediation_hint(error_class: str, stop_reason: str, stage: str = "cad_llm") -> str:
    """修复建议（统一函数，CAD/Mesh 通用）。"""
    if stop_reason == "failure_class_not_allowed":
        return f"Error class '{error_class}' is out of repair scope; manual intervention required."
    if stop_reason == "repeated_failure_limit":
        return f"Same failure repeated. Inspect audit log ({stage}_repair_audit.json) and refine prompt/constraints."
    stage_map = _REMEDIATION_HINTS.get(stage, _REMEDIATION_HINTS["cad_llm"])
    return stage_map.get(error_class, f"Inspect {stage}_repair_audit.json for details.")


def classify_failure(log_text: str) -> str:
    """从执行日志文本中推断错误类别（统一分类函数）。"""
    text = log_text.lower()
    if "syntaxerror" in text:
        return ErrorClass.SYNTAX_ERROR
    if "modulenotfounderror" in text or "importerror" in text:
        return ErrorClass.IMPORT_ERROR
    if "filenotfounderror" in text:
        return ErrorClass.FILE_NOT_FOUND
    if "geometryexception" in text or "shape is null" in text or "invalid geometry" in text:
        return ErrorClass.GEOMETRIC_INVALID
    return ErrorClass.RUNTIME_ERROR


def extract_error_message(log_text: str, max_len: int = 300) -> str:
    """从日志文本中提取最后一条有效错误信息。"""
    lines = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
    if not lines:
        return "unknown runtime error"
    return lines[-1][:max_len]


def build_issue_report(
    *,
    stage: str,
    stop_reason: str,
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    """构建统一格式的 issue_report 字典（供 issue_report.json 消费）。

    M2.4 完成标准：issue_report 可直接消费修复历史。
    """
    last = attempts[-1] if attempts else {}
    err_class = str(last.get("error_class", ErrorClass.RUNTIME_ERROR))
    err_msg = str(last.get("error_message", "unknown error"))
    return {
        "error_stage": stage,
        "error_class": err_class,
        "error_message": err_msg,
        "root_cause_hint": root_cause_hint(err_class, stage=stage),
        "remediation_hint": remediation_hint(err_class, stop_reason, stage=stage),
        "stop_reason": stop_reason,
        "repair_history_summary": {
            "total_attempts": len(attempts),
            "attempt_results": [
                {"attempt": a.get("attempt"), "result": a.get("round_result"), "error_class": a.get("error_class")}
                for a in attempts
            ],
        },
    }
