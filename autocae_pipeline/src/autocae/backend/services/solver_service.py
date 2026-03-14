"""Solver service — 求解器适配和执行服务（Stages 5 & 6）。

包含：
  - CalculiXAdapter  : AnalysisModel + MeshGroups → job.inp
  - SolverRunner     : 执行 ccx 子进程，跟踪运行状态
"""

from __future__ import annotations

import os
import subprocess
import textwrap
import time
from datetime import datetime
from io import StringIO
from pathlib import Path

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
from autocae.schemas.solver import (
    ResourceLimits,
    RunStatus,
    RunStatusEnum,
    SolverJob,
    SolverProfile,
    SolverType,
)


# ---------------------------------------------------------------------------
# CalculiX Adapter — AnalysisModel → job.inp
# ---------------------------------------------------------------------------

class CalculiXAdapter:
    """生成 CalculiX .inp 输入卡片。"""

    solver_type = SolverType.CALCULIX.value

    def write_input(
        self,
        analysis_model: AnalysisModel,
        mesh_groups: MeshGroups,
        output_dir: Path,
    ) -> list[Path]:
        """生成 job.inp 文件。"""
        output_dir.mkdir(parents=True, exist_ok=True)
        inp_path = output_dir / "job.inp"
        mesh_inp = Path(mesh_groups.mesh_file)

        buf = StringIO()
        w = buf.write

        w("** AutoCAE Pipeline — CalculiX Input Deck\n")
        w(f"** Analysis ID : {analysis_model.metadata.analysis_id}\n")
        w(f"** Mesh file   : {mesh_inp.name}\n")
        w("**\n")

        try:
            rel_mesh = mesh_inp.relative_to(output_dir)
        except ValueError:
            rel_mesh = mesh_inp
        w(f"*INCLUDE, INPUT={rel_mesh}\n")
        w("**\n")

        # Gmsh 仅导出 ELSET；CalculiX *BOUNDARY/*CLOAD 需要 NSET
        # 解析 mesh.inp，为每个命名 ELSET 生成对应的 NSET
        nset_block = self._build_nsets_from_mesh(mesh_groups.mesh_file)
        if nset_block:
            w(nset_block)
            w("**\n")

        # 检查是否存在复合材料截面
        # CalculiX COMPOSITE 选项仅支持 S8R 元素；当网格含 CPS3/C3D4 等非 S8R 元素时，
        # 自动退化为 CLT（经典层合板理论）等效正交各向异性材料 + SOLID SECTION，
        # 对面内拉伸/压缩分析结果等价，且不依赖元素阶次。
        has_composite = any(
            s.section_type == SectionType.COMPOSITE_SHELL and s.layup
            for s in analysis_model.sections
        )
        mesh_has_s8r = self._mesh_has_s8r(mesh_groups.mesh_file)
        use_clt_fallback = has_composite and not mesh_has_s8r

        # 计算等效 CLT 材料（复合截面 → 等效各向异性膜材料）
        mat_by_id = {m.material_id: m for m in analysis_model.materials}
        equiv_mats: dict[str, CanonicalMaterial] = {}
        if use_clt_fallback:
            for sec in analysis_model.sections:
                if sec.section_type == SectionType.COMPOSITE_SHELL and sec.layup:
                    em = self._clt_equivalent_material(sec, mat_by_id)
                    equiv_mats[sec.section_id] = em

        # 写材料块（原始材料 + CLT 等效材料）
        for mat in analysis_model.materials:
            w(self._material_block(mat))
        for em in equiv_mats.values():
            w(self._material_block(em))

        # 若使用 S8R，生成 *ORIENTATION 块（COMPOSITE 截面需要）
        if not use_clt_fallback:
            ori_map = self._collect_orientations(analysis_model)
            for ori_block in ori_map.values():
                w(ori_block)
        else:
            ori_map = {}

        for sec in analysis_model.sections:
            w(self._section_block(sec, ori_map, equiv_mats))

        for step in analysis_model.analysis_steps:
            w(self._step_block(step, analysis_model, mesh_groups))

        inp_path.write_text(buf.getvalue(), encoding="utf-8")
        logger.info(f"CalculiX input deck written → {inp_path}")
        return [inp_path]

    def _mesh_has_s8r(self, mesh_file: str) -> bool:
        """检查 mesh.inp 中是否包含 S8R 元素（CalculiX COMPOSITE 截面的唯一合法元素）。"""
        try:
            with open(mesh_file, encoding="latin-1", errors="replace") as fh:
                for line in fh:
                    if "S8R" in line.upper() and "*ELEMENT" in line.upper():
                        return True
            return False
        except Exception:
            return False

    @staticmethod
    def _parse_elem_nodes(lines: list[str]) -> dict[int, list[int]]:
        """从 .inp 行列表中解析 *ELEMENT 块，返回 element_id → node_ids 映射。"""
        elem_nodes: dict[int, list[int]] = {}
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.upper().startswith("*ELEMENT"):
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("*"):
                    parts = lines[i].strip().rstrip(",").split(",")
                    try:
                        parts_int = [int(p.strip()) for p in parts if p.strip()]
                        if parts_int:
                            eid = parts_int[0]
                            elem_nodes[eid] = parts_int[1:]
                    except ValueError:
                        pass
                    i += 1
                continue
            i += 1
        return elem_nodes

    def _build_nsets_from_mesh(self, mesh_file: str) -> str:
        """解析 mesh.inp，提取各命名 ELSET 的节点，生成对应 *NSET 块。

        Gmsh 导出 .inp 时只生成 *ELSET（单元集），不生成 *NSET（节点集）。
        CalculiX 的 *BOUNDARY 和 *CLOAD 必须引用 NSET，因此需要手动构造。

        解析流程：
          1. 收集所有 *ELEMENT 定义（element_id → node_ids 列表）
          2. 收集所有命名 *ELSET（ELSET_name → element_id 列表）
          3. 对每个命名 ELSET，展开得到节点 ID 集合
          4. 若某 ELSET 的元素在当前 mesh.inp 中不存在（如 CPS3 被剥离后），
             尝试读取 mesh.inp.bak（含 CPS3 面元素）补充节点信息
          5. 生成 *NSET, NSET=<name> 块
        """
        # element_id → [node_ids]
        elem_nodes: dict[int, list[int]] = {}
        # elset_name → [element_ids]
        elsets: dict[str, list[int]] = {}

        current_elset: str | None = None
        current_element_nodes: list[int] = []
        collecting_elset_ids = False

        try:
            with open(mesh_file, encoding="latin-1", errors="replace") as fh:
                lines = fh.readlines()
        except Exception as exc:
            logger.warning(f"_build_nsets_from_mesh: cannot read {mesh_file}: {exc}")
            return ""

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            upper = line.upper()

            if upper.startswith("*ELEMENT"):
                collecting_elset_ids = False
                # 提取 ELSET 名（若有）—— 本循环主要收集 elem_id → nodes
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("*"):
                    parts = lines[i].strip().rstrip(",").split(",")
                    try:
                        parts_int = [int(p.strip()) for p in parts if p.strip()]
                        if parts_int:
                            eid = parts_int[0]
                            elem_nodes[eid] = parts_int[1:]
                    except ValueError:
                        pass
                    i += 1
                continue

            if upper.startswith("*ELSET"):
                collecting_elset_ids = False
                # 提取 ELSET 名称
                name_match = None
                for part in line.split(","):
                    p = part.strip()
                    if p.upper().startswith("ELSET="):
                        name_match = p.split("=", 1)[1].strip()
                        break
                if name_match:
                    current_elset = name_match
                    elsets.setdefault(current_elset, [])
                    collecting_elset_ids = True
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("*"):
                    if collecting_elset_ids and current_elset:
                        for tok in lines[i].strip().rstrip(",").split(","):
                            tok = tok.strip()
                            if tok:
                                try:
                                    elsets[current_elset].append(int(tok))
                                except ValueError:
                                    pass
                    i += 1
                continue

            i += 1

        # 若某些 ELSET 的元素 ID 在 elem_nodes 中不存在（例如 CPS3 面元素已被剥离），
        # 尝试读取 mesh.inp.bak（含原始 CPS3 元素）补充节点信息
        unresolved_eids = set()
        for eset_name, eids in elsets.items():
            for eid in eids:
                if eid not in elem_nodes:
                    unresolved_eids.add(eid)
        if unresolved_eids:
            bak_path = Path(mesh_file).with_suffix(".inp.bak")
            if bak_path.exists():
                try:
                    with open(bak_path, encoding="latin-1", errors="replace") as fh:
                        bak_lines = fh.readlines()
                    bak_elem_nodes = self._parse_elem_nodes(bak_lines)
                    # 仅补充缺失的元素（不覆盖已有的）
                    for eid, nids in bak_elem_nodes.items():
                        if eid not in elem_nodes:
                            elem_nodes[eid] = nids
                    logger.info(
                        f"  Loaded {len(bak_elem_nodes)} elements from backup mesh "
                        f"to resolve {len(unresolved_eids)} missing element IDs"
                    )
                except Exception as exc:
                    logger.warning(f"  Could not read backup mesh {bak_path}: {exc}")

        # 为每个命名 ELSET 构建 NSET
        # 跳过纯编号的（如 Surface1, Volume1 等 Gmsh 自动生成的）
        skip_prefixes = ("SURFACE", "VOLUME")
        buf = StringIO()
        for eset_name, eids in elsets.items():
            if eset_name.upper().startswith(skip_prefixes):
                continue
            nodes: set[int] = set()
            for eid in eids:
                nodes.update(elem_nodes.get(eid, []))
            if not nodes:
                continue
            sorted_nodes = sorted(nodes)
            buf.write(f"*NSET, NSET={eset_name}\n")
            # 每行最多 16 个节点 ID（CalculiX 格式约定）
            row: list[str] = []
            for nid in sorted_nodes:
                row.append(str(nid))
                if len(row) == 16:
                    buf.write(", ".join(row) + "\n")
                    row = []
            if row:
                buf.write(", ".join(row) + "\n")
        result = buf.getvalue()
        if result:
            logger.info(f"  Generated NSETs from {len(elsets)} ELSETs in mesh")
        return result

    def _clt_equivalent_material(
        self,
        sec: Section,
        mat_by_id: dict[str, CanonicalMaterial],
    ) -> CanonicalMaterial:
        """经典层合板理论（CLT）A-矩阵 → 等效面内正交各向异性材料。

        对于平面应力单元（CPS3/CPS4），CalculiX 不支持 COMPOSITE 截面，
        本方法将多铺层属性浓缩为一个等效单层正交各向异性材料，
        使面内刚度（拉伸、压缩、剪切）与原层合板吻合。

        推导：
            A_ij = Σ Q̄_ij(θ_k) * t_k   (各层分贡献)
            E_x   = (A11*A22 - A12²)/(A22 * t)
            E_y   = (A11*A22 - A12²)/(A11 * t)
            G_xy  = A66 / t
            ν_xy  = A12 / A22
        """
        import math

        # 取第一层材料作为基体材料（各层通常同材料）
        fallback_mat = next(iter(mat_by_id.values())) if mat_by_id else None

        a11 = a12 = a22 = a66 = 0.0
        total_t = sum(ply.thickness for ply in sec.layup)

        for ply in sec.layup:
            mat = mat_by_id.get(ply.material_id) or fallback_mat
            if mat is None:
                continue
            E1  = mat.E1 or mat.E or 1.0
            E2  = mat.E2 or E1
            nu12 = mat.nu12 or mat.nu or 0.3
            G12  = mat.G12 or E1 / (2.0 * (1.0 + nu12))
            nu21 = nu12 * E2 / E1
            denom = max(1.0 - nu12 * nu21, 1e-12)
            Q11 = E1 / denom
            Q22 = E2 / denom
            Q12 = nu12 * Q22
            Q66 = G12

            theta = math.radians(ply.angle)
            c, s = math.cos(theta), math.sin(theta)
            c2, s2 = c * c, s * s
            c4, s4 = c2 * c2, s2 * s2
            c2s2 = c2 * s2

            Q11b = Q11 * c4 + 2.0 * (Q12 + 2.0 * Q66) * c2s2 + Q22 * s4
            Q22b = Q11 * s4 + 2.0 * (Q12 + 2.0 * Q66) * c2s2 + Q22 * c4
            Q12b = (Q11 + Q22 - 4.0 * Q66) * c2s2 + Q12 * (c4 + s4)
            Q66b = (Q11 + Q22 - 2.0 * Q12 - 2.0 * Q66) * c2s2 + Q66 * (c4 + s4)

            t = ply.thickness
            a11 += Q11b * t
            a12 += Q12b * t
            a22 += Q22b * t
            a66 += Q66b * t

        t = max(total_t, 1e-12)
        denom = max(a11 * a22 - a12 ** 2, 1e-12)
        Ex   = denom / (a22 * t)
        Ey   = denom / (a11 * t)
        Gxy  = a66 / t
        nuxy = a12 / max(a22, 1e-12)
        # 估算平均密度
        rho_avg: float | None = None
        if fallback_mat and fallback_mat.rho is not None:
            rho_avg = fallback_mat.rho

        equiv_id = f"clt_equiv_{sec.material_id}"
        logger.info(
            f"CLT fallback for section '{sec.section_id}': "
            f"Ex={Ex:.0f} Ey={Ey:.0f} Gxy={Gxy:.0f} nuxy={nuxy:.4f} MPa"
        )
        return CanonicalMaterial(
            material_id=equiv_id,
            name=f"CLT-equiv({sec.material_id})",
            E1=Ex, E2=Ey, E3=Ey,
            nu12=nuxy, nu13=nuxy, nu23=nuxy,
            G12=Gxy, G13=Gxy, G23=Gxy,
            rho=rho_avg,
        )

    def _collect_orientations(self, analysis_model: AnalysisModel) -> dict[str, str]:
        """收集所有复合截面中的唯一铺层角度，返回 {orientation_name: *ORIENTATION块} 字典。

        CalculiX *SHELL SECTION COMPOSITE 第4列必须是 *ORIENTATION 名称，
        本方法为每个唯一角度生成一个 RECTANGULAR 坐标系方向定义。

        坐标系规则（平面壳单元，全局 z 为法线方向）：
            局部1轴 = (cos θ, sin θ, 0)   — 纤维方向
            局部2轴 = (-sin θ, cos θ, 0)  — 横向
        方向名称规则：正角 → ORI_<int>（如 ORI_45），负角 → ORI_M<int>（如 ORI_M45）。
        """
        import math

        unique_angles: set[float] = set()
        for sec in analysis_model.sections:
            if sec.layup:
                for ply in sec.layup:
                    unique_angles.add(ply.angle)

        ori_map: dict[str, str] = {}
        for angle in sorted(unique_angles):
            name = self._orientation_name(angle)
            rad = math.radians(angle)
            c, s = math.cos(rad), math.sin(rad)
            # 局部1轴方向向量 (c, s, 0)；局部2轴 (-s, c, 0)（构成右手系）
            block = (
                f"*ORIENTATION, NAME={name}, SYSTEM=RECTANGULAR\n"
                f"{c:.9f},{s:.9f},0., {-s:.9f},{c:.9f},0.\n"
                f"**\n"
            )
            ori_map[name] = block
        return ori_map

    @staticmethod
    def _orientation_name(angle: float) -> str:
        """将角度值转为合法的 CalculiX 方向名称（避免负号和小数点）。

        规则：
            0.0   → ORI_0
            45.0  → ORI_45
            -45.0 → ORI_M45
            90.0  → ORI_90
            22.5  → ORI_22P5
        """
        neg = angle < 0
        abs_angle = abs(angle)
        # 整数角：直接取整数字符串（避免 rstrip 把 90 变成 9）
        if abs_angle == int(abs_angle):
            tag = str(int(abs_angle))
        else:
            tag = f"{abs_angle:.6g}".replace(".", "P")
        return f"ORI_M{tag}" if neg else f"ORI_{tag}"

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
                threads=analysis_model.solver_extensions.calculix.get("threads", 1),
            ),
            resource_limits=ResourceLimits(),
        )
        job_path = output_dir / "solver_job.json"
        job_path.write_text(job.to_json(), encoding="utf-8")
        logger.info(f"solver_job.json saved → {job_path}")
        return job

    # ------------------------------------------------------------------
    # 私有方法：CalculiX 关键字块生成器
    # ------------------------------------------------------------------

    def _material_block(self, mat: CanonicalMaterial) -> str:
        """生成 *MATERIAL 关键字块（各向同性或正交各向异性）。

        各向同性：E + nu → *ELASTIC
        正交各向异性：E1/E2/G12... → *ELASTIC, TYPE=ORTHO
        有密度 rho 时额外写 *DENSITY（模态分析必需）。
        """
        lines: list[str] = []
        lines.append(f"*MATERIAL, NAME={mat.material_id.upper()}")
        if mat.E is not None and mat.nu is not None:
            # 各向同性：杨氏模量 + 泊松比
            lines.append("*ELASTIC")
            lines.append(f"{mat.E:.6e}, {mat.nu:.4f}")
        elif mat.E1 is not None:
            # 正交各向异性（复合材料）：需要 9 个弹性常数
            E1 = mat.E1; E2 = mat.E2 or mat.E1
            E3 = mat.E3 or E2
            nu12 = mat.nu12 or 0.0; nu13 = mat.nu13 or nu12; nu23 = mat.nu23 or nu12
            G12 = mat.G12 or E1 / (2 * (1 + nu12))  # 未给定时用各向同性公式估算
            G13 = mat.G13 or G12; G23 = mat.G23 or G12
            lines.append("*ELASTIC, TYPE=ORTHO")
            # CalculiX *ELASTIC, TYPE=ORTHO 参数顺序：
            # Line 1: E1, E2, nu12, G12, E3, nu13, G13, nu23
            # Line 2: G23
            lines.append(
                f"{E1:.6e}, {E2:.6e}, {nu12:.4f}, {G12:.6e}, "
                f"{E3:.6e}, {nu13:.4f}, {G13:.6e}, {nu23:.4f}"
            )
            lines.append(f"{G23:.6e}")
        if mat.rho is not None:
            # 密度（模态/动力分析必须提供）
            lines.append("*DENSITY")
            lines.append(f"{mat.rho:.6e}")
        lines.append("**")
        return "\n".join(lines) + "\n"

    def _section_block(
        self,
        sec: Section,
        ori_map: dict[str, str] | None = None,
        equiv_mats: dict[str, CanonicalMaterial] | None = None,
    ) -> str:
        """生成截面属性关键字块（*SHELL SECTION / *SOLID SECTION）。

        SHELL:            均匀壳截面，需指定厚度
        COMPOSITE_SHELL:
          - S8R 元素（S8R 网格）：*SHELL SECTION COMPOSITE + *ORIENTATION
          - 其他元素（CPS3/C3D4 等）：CLT 等效正交各向异性 *SOLID SECTION
        SOLID/BEAM:       实体/梁截面，直接引用材料
        """
        lines: list[str] = []
        mat_name = sec.material_id.upper()
        set_name = sec.region_ref.upper()

        if sec.section_type == SectionType.SHELL:
            t = sec.thickness or 1.0
            lines.append(f"*SHELL SECTION, ELSET={set_name}, MATERIAL={mat_name}")
            lines.append(f"{t:.6f}")

        elif sec.section_type == SectionType.COMPOSITE_SHELL:
            if equiv_mats and sec.section_id in equiv_mats:
                # CLT 退化模式：非 S8R 网格 → 等效正交各向异性 *SOLID SECTION
                em = equiv_mats[sec.section_id]
                total_t = sum(p.thickness for p in sec.layup) if sec.layup else (sec.thickness or 1.0)
                lines.append(
                    f"** CLT-equivalent section (original: COMPOSITE {len(sec.layup)} plies)"
                )
                lines.append(
                    f"*SOLID SECTION, ELSET={set_name}, MATERIAL={em.material_id.upper()}"
                )
                lines.append(f"{total_t:.6f}")
            else:
                # S8R 模式：标准 COMPOSITE 截面（每行一铺层，第4列为方向名称）
                lines.append(f"*SHELL SECTION, ELSET={set_name}, COMPOSITE")
                for ply in sec.layup:
                    ply_mat = ply.material_id.upper()
                    ori_name = self._orientation_name(ply.angle)
                    lines.append(f"{ply.thickness:.6f}, , {ply_mat}, {ori_name}")

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
        """生成 *STEP ... *END STEP 块（包含分析控制、边界条件、载荷、输出请求）。

        各分析类型对应的 CalculiX 关键字：
            STATIC   → *STATIC   inc_initial, time_period, inc_min, inc_max
            BUCKLE   → *BUCKLE   n_modes, 0, , , 30（Lanczos 参数）
            FREQUENCY → *FREQUENCY  n_modes
            DYNAMIC  → *DYNAMIC  参数同 STATIC
        """
        lines: list[str] = []
        nlgeom_str = ", NLGEOM=YES" if step.nlgeom else ""
        lines.append(f"*STEP{nlgeom_str}")
        lines.append(f"** Step: {step.step_name}")

        if step.step_type == AnalysisStepType.STATIC:
            lines.append("*STATIC")
            lines.append(
                f"{step.inc_initial:.6e}, {step.time_period:.6e}, "
                f"{step.inc_min:.6e}, {step.inc_max:.6e}"
            )
        elif step.step_type == AnalysisStepType.BUCKLE:
            n = step.num_eigenmodes or 5  # 默认提取 5 阶屈曲模态
            lines.append("*BUCKLE")
            lines.append(f"{n}, 0, , , 30")
        elif step.step_type == AnalysisStepType.FREQUENCY:
            n = step.num_frequencies or 10  # 默认提取前 10 阶固有频率
            lines.append("*FREQUENCY")
            lines.append(f"{n}")
        elif step.step_type == AnalysisStepType.DYNAMIC:
            lines.append("*DYNAMIC")
            lines.append(
                f"{step.inc_initial:.6e}, {step.time_period:.6e}, "
                f"{step.inc_min:.6e}, {step.inc_max:.6e}"
            )

        # 写入边界条件（*BOUNDARY）
        for bc in model.boundary_conditions:
            lines += self._bc_lines(bc)
        # 写入载荷（*CLOAD / *DLOAD）
        for ld in model.loads:
            lines += self._load_lines(ld)

        # 写入场输出请求（*NODE FILE / *EL FILE）
        or_ = model.output_requests
        lines.append("*NODE FILE")
        lines.append(", ".join(or_.field_outputs))
        lines.append("*EL FILE")
        # 单元场输出中排除节点量 U 和 RF（这些只在 NODE FILE 中输出）
        lines.append(", ".join(f for f in or_.field_outputs if f not in ("U", "RF")))
        if or_.history_outputs:
            # 历程输出（节点打印）：用于生成载荷-位移曲线
            for set_ref in or_.history_set_refs:
                lines.append(f"*NODE PRINT, NSET={set_ref.upper()}")
                lines.append(", ".join(or_.history_outputs))

        lines.append("*END STEP")
        lines.append("**")
        return "\n".join(lines) + "\n"

    def _bc_lines(self, bc: BoundaryConditionDef) -> list[str]:
        """生成 *BOUNDARY 关键字行（逐 DOF 写入位移约束）。

        CalculiX *BOUNDARY 格式：
            SET_NAME, first_dof, last_dof, displacement_value
        若 constrained_dofs 为空，则生成全约束行（DOF 1-6 = 0）。
        """
        lines = ["*BOUNDARY"]
        set_name = bc.set_ref.upper()
        if bc.constrained_dofs:
            # 逐自由度写入（允许每个 DOF 设置不同位移值）
            for dof in bc.constrained_dofs:
                val = bc.displacement_values.get(dof, 0.0)
                lines.append(f"{set_name}, {dof}, {dof}, {val:.6e}")
        else:
            # 全约束（1~6 自由度全部固定为 0）
            lines.append(f"{set_name}, 1, 6, 0.0")
        return lines

    def _load_lines(self, ld: LoadDef) -> list[str]:
        """生成载荷关键字行（*CLOAD 集中力 / *DLOAD 分布压强）。

        拉/压/剪：*CLOAD（集中力，按方向分量分解）
        压强：    *DLOAD, P（均布压强，施加于指定面）
        弯/矩：   *CLOAD（集中力矩，DOF 4-6 为转动自由度）
        """
        lines: list[str] = []
        set_name = ld.set_ref.upper()
        ltype = ld.load_type.lower()

        if ltype in ("tension", "compression", "shear"):
            # 集中力：将载荷量值按方向向量分解为 X/Y/Z 分量
            mag = ld.magnitude
            d = ld.direction
            lines.append("*CLOAD")
            for i, comp in enumerate(d, start=1):
                if abs(comp) > 1e-10:  # 跳过接近零的分量（避免写冗余行）
                    lines.append(f"{set_name}, {i}, {mag * comp:.6e}")
        elif ltype == "pressure":
            # 均布压强：格式 SET_NAME, P, pressure_value
            lines.append("*DLOAD")
            lines.append(f"{set_name}, P, {ld.magnitude:.6e}")
        elif ltype in ("bending", "moment"):
            # 集中力矩：DOF 从 4 开始（4=Rx, 5=Ry, 6=Rz）
            mag = ld.magnitude
            d = ld.direction
            lines.append("*CLOAD")
            for i, comp in enumerate(d, start=4):
                if abs(comp) > 1e-10:
                    lines.append(f"{set_name}, {i}, {mag * comp:.6e}")
        return lines


# ---------------------------------------------------------------------------
# 求解器可执行文件注册表：SolverType → 命令名（需在 PATH 中）
# ---------------------------------------------------------------------------

_SOLVER_EXECUTABLES: dict[SolverType, str] = {
    SolverType.CALCULIX: "ccx",  # CalculiX 开源求解器（需安装并加入 PATH）
}


class SolverRunner:
    """执行求解器子进程并跟踪运行状态。

    Usage::

        runner = SolverRunner(ccx_executable="ccx")
        run_status = runner.run(solver_job)

    ccx_executable 优先级（由高到低）：
        1. 构造函数 ccx_executable 参数
        2. 环境变量 CCX_PATH（如 export CCX_PATH=/opt/ccx/bin/ccx）
        3. 注册表默认值（命令名 "ccx"，须在 PATH 中）
    """

    def __init__(self, ccx_executable: str | None = None) -> None:
        self._ccx_executable = ccx_executable or os.environ.get("CCX_PATH") or None
        # 预计算 DLL 搜索路径（bConverged Windows 包结构：bin/ 与 ccx/ 同级）
        self._extra_path_dirs: list[str] = []
        if self._ccx_executable:
            exe = Path(self._ccx_executable)
            # 添加 exe 自身所在目录及其父目录下的 bin/（兼容 bConverged 安装布局）
            self._extra_path_dirs.append(str(exe.parent))
            sibling_bin = exe.parent.parent / "bin"
            if sibling_bin.is_dir():
                self._extra_path_dirs.append(str(sibling_bin))

    def run(self, job: SolverJob) -> RunStatus:
        """启动求解器并返回 RunStatus。

        流程：
            1. 查找对应求解器的可执行文件名
            2. 构造命令行；多线程通过 env['OMP_NUM_THREADS'] 设置（兼容 Windows）
            3. 将 stdout/stderr 重定向到 ccx_run.log
            4. 检查返回码和结果文件，决定 COMPLETED/FAILED/ABORTED
            5. 将 RunStatus 写入 run_status.json
        """
        status = RunStatus(job_id=job.job_id, status=RunStatusEnum.RUNNING)
        status.start_time = datetime.utcnow()

        # 确定求解器可执行文件路径：
        #   self._ccx_executable（构造函数 / CCX_PATH 环境变量）> 注册表默认值
        if self._ccx_executable:
            executable = self._ccx_executable
        else:
            executable = _SOLVER_EXECUTABLES.get(job.solver_type)
        if executable is None:
            status.status = RunStatusEnum.FAILED
            status.error_message = f"No executable registered for solver type '{job.solver_type}'"
            return status

        # 工作目录：解析为绝对路径（solver_job.json 可能存储相对路径）
        work_dir = Path(job.working_dir).resolve()
        inp_file = Path(job.input_files[0])
        # CalculiX 需要的 job_name 只是文件名 stem（不含路径，从 cwd 中查找）
        job_name = inp_file.stem
        log_path = work_dir / "ccx_run.log"

        # 构造命令行（不在命令前拼 OMP_NUM_THREADS=N，Windows 不支持该写法）
        cmd = [executable, job_name]

        # 多线程：通过进程环境变量传递（跨平台兼容）
        run_env = os.environ.copy()
        if job.profile.threads > 1:
            run_env["OMP_NUM_THREADS"] = str(job.profile.threads)
        # 注入额外 DLL/依赖路径（bConverged Windows 包需要 bin/ 目录在 PATH 中）
        if self._extra_path_dirs:
            sep = ";" if os.name == "nt" else ":"
            run_env["PATH"] = sep.join(self._extra_path_dirs) + sep + run_env.get("PATH", "")

        logger.info(
            f"SolverRunner: executing '{' '.join(cmd)}' in {work_dir} "
            f"(OMP_NUM_THREADS={job.profile.threads})"
        )
        t0 = time.perf_counter()

        try:
            with open(log_path, "w", encoding="utf-8") as log_fh:
                proc = subprocess.run(
                    cmd,
                    cwd=str(work_dir),
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,     # stderr 合并到 stdout 日志
                    env=run_env,
                    timeout=job.resource_limits.max_wall_time_s,
                )
            return_code = proc.returncode

        except FileNotFoundError:
            # 求解器可执行文件不存在（未安装或路径配置错误）
            status.status = RunStatusEnum.FAILED
            status.error_message = (
                f"Solver executable '{executable}' not found. "
                "Please install CalculiX (ccx) and add it to PATH, "
                "or set the CCX_PATH environment variable."
            )
            status.end_time = datetime.utcnow()
            return status

        except subprocess.TimeoutExpired:
            # 超过最大挂钟时间，强制终止
            status.status = RunStatusEnum.ABORTED
            status.error_message = "Solver exceeded maximum wall time."
            status.end_time = datetime.utcnow()
            return status

        elapsed = time.perf_counter() - t0
        status.end_time = datetime.utcnow()
        status.wall_time_s = elapsed
        status.return_code = return_code
        status.log_file = str(log_path)

        # 收集结果文件（.frd=场结果, .dat=文本摘要, .cvg=收敛历程）
        result_files: list[str] = []
        for ext in (".frd", ".dat", ".cvg"):
            rfile = work_dir / f"{job_name}{ext}"
            if rfile.exists():
                result_files.append(str(rfile))
        status.result_files = result_files

        if return_code == 0 and result_files:
            # 返回码为 0 且存在结果文件 → 求解成功
            status.status = RunStatusEnum.COMPLETED
            logger.info(
                f"Solver completed successfully in {elapsed:.1f}s. "
                f"Result files: {result_files}"
            )
        else:
            # 非零返回码或缺少结果文件 → 求解失败
            status.status = RunStatusEnum.FAILED
            status.failure_stage = "solver_execution"
            status.error_message = f"Solver returned non-zero exit code: {return_code}"
            logger.error(status.error_message)

        # 将最终状态写入 run_status.json（供后处理阶段读取）
        status_path = work_dir / "run_status.json"
        status_path.write_text(status.to_json(), encoding="utf-8")
        logger.info(f"run_status.json saved → {status_path}")
        return status
