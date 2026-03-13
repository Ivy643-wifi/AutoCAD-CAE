"""Solver job and run status schemas.

solver_job.json  – SolverAdapter 生成的求解任务配置（描述"怎么运行求解器"）。
run_status.json  – SolverRunner 在求解过程中/之后写入的运行状态。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SolverType(str, Enum):
    """支持的求解器类型（设计原则 G-01：流水线与求解器解耦）。"""
    CALCULIX = "calculix"    # 开源有限元求解器，Abaqus 兼容输入格式
    ABAQUS = "abaqus"        # 商业有限元软件（未来扩展）
    NASTRAN = "nastran"      # 商业有限元软件（未来扩展）
    CODE_ASTER = "code_aster"  # 开源有限元软件（未来扩展）


class RunStatusEnum(str, Enum):
    """求解器运行状态枚举。"""
    PENDING = "pending"       # 等待运行
    RUNNING = "running"       # 正在运行
    COMPLETED = "completed"   # 成功完成
    FAILED = "failed"         # 运行失败（非零返回码或找不到结果文件）
    ABORTED = "aborted"       # 超时被强制终止


class ResourceLimits(BaseModel):
    """求解资源限制：防止单个算例占用过多计算资源。"""
    max_wall_time_s: int = Field(default=3600, description="最大挂钟时间 [s]（默认 1 小时）")
    max_memory_mb: int = Field(default=4096, description="最大内存 [MB]（默认 4 GB）")
    num_cpus: int = Field(default=1, ge=1, description="CPU 核数")


class SolverProfile(BaseModel):
    """求解器运行配置：线程数、特殊标志等。"""
    profile_name: str = "default"
    threads: int = Field(default=1, ge=1, description="并行线程数（OMP_NUM_THREADS）")
    solver_flags: list[str] = Field(default_factory=list, description="额外的命令行参数")
    extra: dict[str, Any] = Field(default_factory=dict)


class SolverJob(BaseModel):
    """solver_job.json — 求解任务配置对象。

    由 SolverAdapter（如 CalculiXAdapter）在生成 job.inp 之后创建，
    然后传给 SolverRunner 去实际执行求解。

    包含：
        - 使用哪个求解器（solver_type）
        - 输入文件在哪（input_files）
        - 工作目录（working_dir）
        - 资源限制（resource_limits）
    """

    job_id: str = Field(default_factory=lambda: f"job_{uuid.uuid4().hex[:8]}")
    analysis_id: str                  # 对应 AnalysisModel.metadata.analysis_id
    solver_type: SolverType           # 使用的求解器类型
    input_files: list[str] = Field(description="求解器输入文件路径列表（如 job.inp）")
    working_dir: str                  # 工作目录（存放输入/输出文件）
    profile: SolverProfile = Field(default_factory=SolverProfile)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "SolverJob":
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class RunStatus(BaseModel):
    """run_status.json — 求解器运行状态。

    由 SolverRunner 在求解完成后写入，PostprocessEngine 读取以判断是否有可用结果。

    关键字段：
        status：最终状态（COMPLETED 才表示有结果可解析）
        result_files：求解器输出的结果文件列表（CalculiX 生成 .frd, .dat, .cvg）
        log_file：求解器日志文件（调试用，DiagnosticsValidator 会解析它）
        wall_time_s：实际求解耗时
    """

    job_id: str
    status: RunStatusEnum = RunStatusEnum.PENDING
    start_time: datetime | None = None
    end_time: datetime | None = None
    wall_time_s: float | None = None    # 实际耗时 [s]
    return_code: int | None = None      # 求解器进程返回码（0=正常退出）
    result_files: list[str] = Field(
        default_factory=list,
        description="结果文件路径列表（.frd=场结果, .dat=文本摘要, .cvg=收敛历程）",
    )
    log_file: str | None = None         # 求解器日志路径（ccx_run.log）
    failure_stage: str | None = None    # 失败发生在哪个阶段
    error_message: str = ""             # 错误描述
    warnings: list[str] = Field(default_factory=list)
    convergence_info: dict[str, Any] = Field(default_factory=dict)  # 收敛信息摘要

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "RunStatus":
        from pathlib import Path
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
