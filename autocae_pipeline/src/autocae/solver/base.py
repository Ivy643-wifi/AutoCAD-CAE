"""Base Solver Adapter — 与求解器无关的接口定义。

设计原则 G-01：流水线主体保持求解器无关性。
CalculiX / Abaqus / Nastran 的差异被 Adapter 层吸收，
主流水线（PipelineRunner）只与 BaseSolverAdapter 接口交互。

如何扩展（接入新求解器）：
    1. 新建 solver/<solver_name>.py
    2. 继承 BaseSolverAdapter，实现 solver_type / write_input() / build_solver_job()
    3. 在 PipelineRunner 中替换 CalculiXAdapter 为新 Adapter 实例
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from autocae.schemas.analysis_model import AnalysisModel
from autocae.schemas.mesh import MeshGroups
from autocae.schemas.solver import SolverJob


class BaseSolverAdapter(ABC):
    """抽象 Solver Adapter：将 AnalysisModel + MeshGroups 转换为求解器输入文件。

    两个必须实现的方法：
        write_input()      → 生成求解器输入文件（如 CalculiX 的 job.inp）
        build_solver_job() → 创建 SolverJob 描述符（供 SolverRunner 执行）
    """

    @property
    @abstractmethod
    def solver_type(self) -> str:
        """返回该 Adapter 对应的求解器类型字符串（如 'calculix'）。"""

    @abstractmethod
    def write_input(
        self,
        analysis_model: AnalysisModel,
        mesh_groups: MeshGroups,
        output_dir: Path,
    ) -> list[Path]:
        """将 AnalysisModel 转换为求解器输入文件，写入 output_dir。

        Args:
            analysis_model: TemplateInstantiator 生成的规范分析模型
            mesh_groups:    MeshBuilder 生成的网格分组（提供集合名和 tag）
            output_dir:     运行目录（写入 job.inp 等）

        Returns:
            写入的输入文件路径列表（CalculiX 通常只有一个 job.inp）
        """

    @abstractmethod
    def build_solver_job(
        self,
        analysis_model: AnalysisModel,
        input_files: list[Path],
        output_dir: Path,
    ) -> SolverJob:
        """创建 SolverJob 描述符，供 SolverRunner 执行。

        Args:
            analysis_model: 分析模型（用于提取 analysis_id 等元信息）
            input_files:    write_input() 返回的输入文件路径列表
            output_dir:     工作目录

        Returns:
            SolverJob 对象（同时写入 solver_job.json）
        """
