"""CalculiX Solver Adapter — 将 AnalysisModel 转换为 CalculiX .inp 输入卡片。

CalculiX 使用 Abaqus 风格的关键字格式（以 * 开头），本模块负责：
    - 生成完整的 job.inp 文件
    - 包含：文件头、*INCLUDE（引用网格）、*MATERIAL、*SECTION、
             *STEP 块（含 *BOUNDARY、载荷、*OUTPUT 请求）
    - 生成 solver_job.json

job.inp 结构示意：
    ** Header（注释）
    *INCLUDE, INPUT=mesh.inp   ← 引用 Gmsh 导出的网格文件
    *MATERIAL, NAME=CARBON_UD  ← 材料定义
      *ELASTIC, TYPE=ORTHO     ← 弹性参数
    *SHELL SECTION / *SOLID SECTION  ← 截面分配
    *STEP [, NLGEOM=YES]       ← 分析步开始
      *STATIC / *BUCKLE / *FREQUENCY  ← 分析类型关键字
      *BOUNDARY               ← 边界条件（节点集 + DOF + 值）
      *CLOAD / *DLOAD         ← 集中载荷 / 分布载荷
      *NODE FILE              ← 场输出请求
      *EL FILE
    *END STEP
"""

from __future__ import annotations

import textwrap
from io import StringIO
from pathlib import Path
from typing import Any

from loguru import logger

from autocae.schemas.analysis_model import (
    AnalysisModel,
    AnalysisStep,
    AnalysisStepType,
    BoundaryConditionDef,
    CanonicalMaterial,
    LoadDef,
    Section,
    SectionType,
)
from autocae.schemas.mesh import MeshGroups
from autocae.schemas.solver import ResourceLimits, SolverJob, SolverProfile, SolverType
from autocae.solver.base import BaseSolverAdapter


class CalculiXAdapter(BaseSolverAdapter):
    """生成 CalculiX .inp 输入卡片的 Adapter 实现。

    使用 StringIO 缓冲区逐块写入，最后一次性写入文件（减少磁盘 IO）。
    """

    solver_type = SolverType.CALCULIX.value

    def write_input(
        self,
        analysis_model: AnalysisModel,
        mesh_groups: MeshGroups,
        output_dir: Path,
    ) -> list[Path]:
        """生成 job.inp 文件（CalculiX 主输入文件）。

        文件结构：
            1. 文件头注释
            2. *INCLUDE 引用网格文件（包含节点坐标和单元连接）
            3. *MATERIAL 块（各向同性或正交各向异性）
            4. *SECTION 块（截面分配）
            5. 各分析步块（*STEP ... *END STEP）

        Returns:
            [job.inp 的 Path 对象]（列表，便于扩展到多输入文件的场景）
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        inp_path = output_dir / "job.inp"
        mesh_inp = Path(mesh_groups.mesh_file)

        buf = StringIO()   # 内存缓冲区，避免频繁磁盘写入
        w = buf.write

        # ----------------------------------------------------------------
        # 1. 文件头（CalculiX 用 ** 表示注释行）
        # ----------------------------------------------------------------
        w("** AutoCAE Pipeline — CalculiX Input Deck\n")
        w(f"** Analysis ID : {analysis_model.metadata.analysis_id}\n")
        w(f"** Mesh file   : {mesh_inp.name}\n")
        w("**\n")

        # ----------------------------------------------------------------
        # 2. 引用网格文件（*INCLUDE）
        # ----------------------------------------------------------------
        # 使用相对路径（若可能），让 job.inp 可移植
        try:
            rel_mesh = mesh_inp.relative_to(output_dir)
        except ValueError:
            rel_mesh = mesh_inp  # 若不在同一目录则使用绝对路径
        w(f"*INCLUDE, INPUT={rel_mesh}\n")
        w("**\n")

        # ----------------------------------------------------------------
        # 3. 材料定义块
        # ----------------------------------------------------------------
        for mat in analysis_model.materials:
            w(self._material_block(mat))

        # ----------------------------------------------------------------
        # 4. 截面定义块
        # ----------------------------------------------------------------
        for sec in analysis_model.sections:
            w(self._section_block(sec))

        # ----------------------------------------------------------------
        # 5. 分析步块（每个 AnalysisStep → 一个 *STEP ... *END STEP 块）
        # ----------------------------------------------------------------
        for step in analysis_model.analysis_steps:
            w(self._step_block(step, analysis_model, mesh_groups))

        inp_path.write_text(buf.getvalue(), encoding="utf-8")
        logger.info(f"CalculiX input deck written → {inp_path}")
        return [inp_path]

    def build_solver_job(
        self,
        analysis_model: AnalysisModel,
        input_files: list[Path],
        output_dir: Path,
    ) -> SolverJob:
        """创建 SolverJob 并写入 solver_job.json。"""
        job = SolverJob(
            analysis_id=analysis_model.metadata.analysis_id,
            solver_type=SolverType.CALCULIX,
            input_files=[str(p) for p in input_files],
            working_dir=str(output_dir),
            profile=SolverProfile(
                profile_name="default",
                # 从 SolverExtensions 读取线程数（默认 1）
                threads=analysis_model.solver_extensions.calculix.get("threads", 1),
            ),
            resource_limits=ResourceLimits(),
        )
        job_path = output_dir / "solver_job.json"
        job_path.write_text(job.to_json(), encoding="utf-8")
        logger.info(f"solver_job.json saved → {job_path}")
        return job

    # ------------------------------------------------------------------
    # 私有：各 CalculiX 关键字块的生成方法
    # ------------------------------------------------------------------

    def _material_block(self, mat: CanonicalMaterial) -> str:
        """生成 *MATERIAL 块。

        各向同性材料：
            *MATERIAL, NAME=STEEL
            *ELASTIC
            210000.0, 0.3
            *DENSITY
            7.85e-9

        正交各向异性材料（复合材料）：
            *MATERIAL, NAME=CARBON_UD
            *ELASTIC, TYPE=ORTHO
            E1, E2, E3, nu12, nu13, nu23, G12, G13
            G23
        """
        lines: list[str] = []
        lines.append(f"*MATERIAL, NAME={mat.name.upper()}")

        if mat.E is not None and mat.nu is not None:
            # 各向同性弹性：E（杨氏模量）和 nu（泊松比）
            lines.append("*ELASTIC")
            lines.append(f"{mat.E:.6e}, {mat.nu:.4f}")
        elif mat.E1 is not None:
            # 正交各向异性弹性（TYPE=ORTHO 需要 9 个独立常数）
            E1  = mat.E1;  E2  = mat.E2 or mat.E1
            E3  = mat.E3 or E2
            nu12 = mat.nu12 or 0.0
            nu13 = mat.nu13 or nu12
            nu23 = mat.nu23 or nu12
            G12 = mat.G12 or E1 / (2 * (1 + nu12))
            G13 = mat.G13 or G12
            G23 = mat.G23 or G12
            lines.append("*ELASTIC, TYPE=ORTHO")
            # CalculiX 格式：第一行 E1,E2,E3,nu12,nu13,nu23,G12,G13；第二行 G23
            lines.append(
                f"{E1:.6e}, {E2:.6e}, {E3:.6e}, "
                f"{nu12:.4f}, {nu13:.4f}, {nu23:.4f}, "
                f"{G12:.6e}, {G13:.6e}"
            )
            lines.append(f"{G23:.6e}")

        if mat.rho is not None:
            # 密度（模态分析必须定义）
            lines.append("*DENSITY")
            lines.append(f"{mat.rho:.6e}")

        lines.append("**")
        return "\n".join(lines) + "\n"

    def _section_block(self, sec: Section) -> str:
        """生成截面属性块（*SHELL SECTION 或 *SOLID SECTION）。

        SHELL 截面：
            *SHELL SECTION, ELSET=SOLID, MATERIAL=STEEL
            2.0   ← 厚度

        COMPOSITE_SHELL 截面（每层铺层一行）：
            *SHELL SECTION, ELSET=SOLID, COMPOSITE
            0.25, , CARBON_UD, 0.0    ← 厚度, 积分点数(留空), 材料名, 角度
            0.25, , CARBON_UD, 45.0

        SOLID 截面：
            *SOLID SECTION, ELSET=SOLID, MATERIAL=STEEL
        """
        lines: list[str] = []
        mat_name = sec.material_id.upper()
        set_name = sec.region_ref.upper()

        if sec.section_type == SectionType.SHELL:
            t = sec.thickness or 1.0
            lines.append(f"*SHELL SECTION, ELSET={set_name}, MATERIAL={mat_name}")
            lines.append(f"{t:.6f}")
        elif sec.section_type == SectionType.COMPOSITE_SHELL:
            lines.append(f"*SHELL SECTION, ELSET={set_name}, COMPOSITE")
            for ply in sec.layup:
                # 格式：厚度, 积分点(留空), 材料名大写, 纤维角度
                lines.append(f"{ply.thickness:.6f}, , {ply.material_id.upper()}, {ply.angle:.2f}")
        elif sec.section_type in (SectionType.SOLID, SectionType.BEAM):
            lines.append(f"*SOLID SECTION, ELSET={set_name}, MATERIAL={mat_name}")

        lines.append("**")
        return "\n".join(lines) + "\n"

    def _step_block(
        self,
        step: AnalysisStep,
        model: AnalysisModel,
        mesh_groups: MeshGroups,
    ) -> str:
        """生成一个 *STEP ... *END STEP 块。

        包含顺序：
            *STEP [, NLGEOM=YES]
            ** Step name 注释
            分析类型关键字（*STATIC / *BUCKLE / *FREQUENCY / *DYNAMIC）
            *BOUNDARY（边界条件）
            *CLOAD / *DLOAD（载荷）
            *NODE FILE / *EL FILE（输出请求）
            *END STEP
        """
        lines: list[str] = []
        nlgeom_str = ", NLGEOM=YES" if step.nlgeom else ""
        lines.append(f"*STEP{nlgeom_str}")
        lines.append(f"** Step: {step.step_name}")

        # 分析类型关键字
        if step.step_type == AnalysisStepType.STATIC:
            lines.append("*STATIC")
            # 格式：初始增量, 分析步总时间, 最小增量, 最大增量
            lines.append(
                f"{step.inc_initial:.6e}, {step.time_period:.6e}, "
                f"{step.inc_min:.6e}, {step.inc_max:.6e}"
            )
        elif step.step_type == AnalysisStepType.BUCKLE:
            n = step.num_eigenmodes or 5  # 默认提取 5 个屈曲模态
            lines.append(f"*BUCKLE")
            # 格式：提取模态数, 向量数(0=自动), 精度(留空), 过滤(留空), 最大迭代
            lines.append(f"{n}, 0, , , 30")
        elif step.step_type == AnalysisStepType.FREQUENCY:
            n = step.num_frequencies or 10  # 默认提取 10 阶频率
            lines.append(f"*FREQUENCY")
            lines.append(f"{n}")
        elif step.step_type == AnalysisStepType.DYNAMIC:
            lines.append("*DYNAMIC")
            lines.append(
                f"{step.inc_initial:.6e}, {step.time_period:.6e}, "
                f"{step.inc_min:.6e}, {step.inc_max:.6e}"
            )

        # 边界条件（*BOUNDARY）
        for bc in model.boundary_conditions:
            lines += self._bc_lines(bc)

        # 载荷（*CLOAD 集中力 / *DLOAD 分布载荷）
        for ld in model.loads:
            lines += self._load_lines(ld)

        # 输出请求（*NODE FILE / *EL FILE）
        or_ = model.output_requests
        lines.append("*NODE FILE")
        lines.append(", ".join(or_.field_outputs))
        lines.append("*EL FILE")
        # 单元场输出排除 U（位移是节点量）和 RF（反力是节点量）
        lines.append(", ".join(f for f in or_.field_outputs if f not in ("U", "RF")))
        if or_.history_outputs:
            for set_ref in or_.history_set_refs:
                lines.append(f"*NODE PRINT, NSET={set_ref.upper()}")
                lines.append(", ".join(or_.history_outputs))

        lines.append("*END STEP")
        lines.append("**")
        return "\n".join(lines) + "\n"

    def _bc_lines(self, bc: BoundaryConditionDef) -> list[str]:
        """生成 *BOUNDARY 关键字行。

        格式：
            *BOUNDARY
            FIXED_END, 1, 1, 0.0   ← 集合名, 起始 DOF, 结束 DOF, 值
            FIXED_END, 2, 2, 0.0
            ...
        """
        lines = [f"*BOUNDARY"]
        set_name = bc.set_ref.upper()
        if bc.constrained_dofs:
            for dof in bc.constrained_dofs:
                val = bc.displacement_values.get(dof, 0.0)
                lines.append(f"{set_name}, {dof}, {dof}, {val:.6e}")
        else:
            # 无指定 DOF → 全约束（DOF 1~6 全为 0）
            lines.append(f"{set_name}, 1, 6, 0.0")
        return lines

    def _load_lines(self, ld: LoadDef) -> list[str]:
        """生成载荷关键字行（*CLOAD 集中力 或 *DLOAD 分布压力）。

        *CLOAD（节点集中力）格式：
            *CLOAD
            LOAD_END, 1, 1.0e+04   ← 集合名, DOF(1=Fx,2=Fy,3=Fz), 量值

        *DLOAD（分布压力）格式：
            *DLOAD
            INNER_SURFACE, P, 1.0e+00   ← 集合名, 压力类型, 量值
        """
        lines: list[str] = []
        set_name = ld.set_ref.upper()
        ltype = ld.load_type.lower()

        if ltype in ("tension", "compression", "shear"):
            # 将量值分解到各方向分量（direction 是单位向量）
            mag = ld.magnitude
            d = ld.direction
            lines.append("*CLOAD")
            for i, comp in enumerate(d, start=1):
                if abs(comp) > 1e-10:   # 忽略近零分量
                    lines.append(f"{set_name}, {i}, {mag * comp:.6e}")
        elif ltype == "pressure":
            lines.append("*DLOAD")
            lines.append(f"{set_name}, P, {ld.magnitude:.6e}")
        elif ltype in ("bending", "moment"):
            mag = ld.magnitude
            d = ld.direction
            lines.append("*CLOAD")
            # DOF 4,5,6 对应力矩分量（Mx, My, Mz）
            for i, comp in enumerate(d, start=4):
                if abs(comp) > 1e-10:
                    lines.append(f"{set_name}, {i}, {mag * comp:.6e}")

        return lines
