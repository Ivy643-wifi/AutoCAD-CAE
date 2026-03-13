"""四层诊断验证器（DiagnosticsValidator）。

诊断分层设计（"早失败"原则：尽可能在最早的层检测到问题）：

    Layer A — 输入验证（CaseSpec schema 合法性 + 业务规则）
              → 由 CaseSpecValidator 负责（case_spec/validator.py）
              → 本模块不重复 Layer A 的工作

    Layer B — 接口验证（文件存在性 + 格式检查）
              → STEP 文件是否存在且非空？
              → mesh.inp / mesh_groups.json 是否存在？
              → 必要的 Physical Group 是否都已创建？

    Layer C — 运行时诊断（求解器日志解析 + 收敛判断）
              → 解析 ccx_run.log，检测 "converged" / "JOB COMPLETED" 关键字
              → 检测 "ERROR" / "FATAL" / "Segmentation" 致命错误

    Layer D — 修复建议（Suggestion）
              → 与 Layer B/C 诊断结果绑定，通过 DiagnosticCheck.suggestion 字段传递
              → 例如："检查边界条件" / "检查 MeshBuilder 的 bounding box 启发式方法"

使用方式：
    validator = DiagnosticsValidator()
    check = validator.check_step_file(step_path)         # Layer B
    checks = validator.parse_ccx_log(log_path)           # Layer C
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.schemas.postprocess import Diagnostics, DiagnosticCheck


class DiagnosticsValidator:
    """运行全部四层诊断，累积并返回 DiagnosticCheck 结果列表。

    典型调用顺序（在 PipelineRunner 中）：
        1. check_step_file()         — CAD 阶段后，验证 model.step
        2. check_mesh_file()         — 网格阶段后，验证 mesh.inp
        3. check_physical_groups()   — 网格阶段后，验证 Physical Groups
        4. parse_ccx_log()           — 求解阶段后，解析收敛日志
    """

    # ------------------------------------------------------------------
    # Layer B — 接口验证
    # ------------------------------------------------------------------

    def check_step_file(self, step_path: str | Path) -> DiagnosticCheck:
        """验证 STEP 文件是否存在且大小合理（Layer B 检查）。

        检查逻辑：
            1. 文件是否存在？
            2. 文件大小是否 >= 100 字节？（过小说明导出失败或为空文件）

        返回：
            DiagnosticCheck（passed=True → 文件正常，passed=False → 有问题）
        """
        p = Path(step_path)
        # 检查 1：文件存在性
        if not p.exists():
            return DiagnosticCheck(
                layer="interface",
                check_name="step_file_exists",
                passed=False,
                message=f"STEP file not found: {p}",
                suggestion="Verify CADBuilder ran successfully and model.step was exported.",
            )
        # 检查 2：文件大小合理性（过小通常意味着 CadQuery 导出失败）
        if p.stat().st_size < 100:
            return DiagnosticCheck(
                layer="interface",
                check_name="step_file_size",
                passed=False,
                message=f"STEP file is suspiciously small ({p.stat().st_size} bytes).",
                suggestion="Re-run CADBuilder and check for CadQuery export errors.",
            )
        # 通过所有检查
        return DiagnosticCheck(
            layer="interface", check_name="step_file_exists", passed=True
        )

    def check_mesh_file(self, mesh_path: str | Path) -> DiagnosticCheck:
        """验证 mesh.inp 网格文件是否存在（Layer B 检查）。

        CalculiX 的 .inp 网格文件由 MeshBuilder 从 Gmsh 导出。
        若文件不存在，说明 Gmsh 网格阶段失败。

        返回：
            DiagnosticCheck（passed=True → 文件存在，passed=False → 文件缺失）
        """
        p = Path(mesh_path)
        if not p.exists():
            return DiagnosticCheck(
                layer="interface",
                check_name="mesh_file_exists",
                passed=False,
                message=f"Mesh file not found: {p}",
                suggestion="Verify MeshBuilder ran successfully.",
            )
        return DiagnosticCheck(
            layer="interface", check_name="mesh_file_exists", passed=True
        )

    def check_physical_groups(
        self, groups_path: str | Path, required_groups: list[str]
    ) -> list[DiagnosticCheck]:
        """验证 mesh_groups.json 中是否包含所有必要的 Physical Group（Layer B 检查）。

        背景：
            CalculiX Adapter（solver/calculix.py）在写 .inp 文件时，依赖 MeshGroups
            中已存在的集合名（如 FIXED_END、LOAD_END）来写边界条件和载荷。
            若 Physical Group 缺失，求解器输入文件将无法正确生成。

        参数：
            groups_path:    mesh_groups.json 文件路径
            required_groups: 必须存在的集合名列表（来自 AnalysisModel 的 BCs 和 Loads）

        返回：
            DiagnosticCheck 列表，每个必要 Group 对应一项
        """
        from autocae.schemas.mesh import MeshGroups

        results: list[DiagnosticCheck] = []
        try:
            # 反序列化 mesh_groups.json → MeshGroups 对象
            mg = MeshGroups.from_json(str(groups_path))
            # 提取所有已创建的求解器集合名
            assigned = {g.solver_set_name for g in mg.groups}

            # 逐一检查每个必要的 Group 是否存在
            for name in required_groups:
                if name not in assigned:
                    results.append(DiagnosticCheck(
                        layer="interface",
                        check_name=f"physical_group_{name}",
                        passed=False,
                        message=f"Required physical group '{name}' not found in mesh.",
                        suggestion=(
                            "Check that the named face heuristics in MeshBuilder "
                            "match the CAD template's bounding box."
                            # 提示：检查 MeshBuilder 的包围盒位置启发式方法
                            # 是否与 CAD 模板的命名面对应
                        ),
                    ))
                else:
                    results.append(DiagnosticCheck(
                        layer="interface",
                        check_name=f"physical_group_{name}",
                        passed=True,
                    ))

        except Exception as exc:
            # mesh_groups.json 解析失败（文件损坏或格式错误）
            results.append(DiagnosticCheck(
                layer="interface",
                check_name="mesh_groups_parseable",
                passed=False,
                message=f"Could not load mesh_groups.json: {exc}",
            ))
        return results

    # ------------------------------------------------------------------
    # Layer C — 运行时诊断
    # ------------------------------------------------------------------

    def parse_ccx_log(self, log_path: str | Path) -> list[DiagnosticCheck]:
        """解析 CalculiX 的 ccx_run.log 文件，提取收敛状态和致命错误（Layer C 检查）。

        检查项目：

        1. 收敛判断（check_name="ccx_convergence"）：
            在日志中搜索关键字：
                - "converged"（增量步收敛标志）
                - "j o b   c o m p l e t e d"（CalculiX 任务完成标志，注意字符间有空格）
            两者任一存在 → 认为收敛成功

        2. 致命错误检测（check_name="ccx_no_fatal_error"）：
            在日志中搜索关键字：
                - "ERROR"（一般错误）
                - "FATAL"（致命错误）
                - "Segmentation"（段错误/崩溃）

        参数：
            log_path: ccx_run.log 文件路径

        返回：
            DiagnosticCheck 列表（最多两项：收敛检查 + 致命错误检查）
        """
        results: list[DiagnosticCheck] = []
        p = Path(log_path)

        # 日志文件不存在 → 无法诊断，返回空列表
        if not p.exists():
            return results

        # 读取日志内容（latin-1 编码，容忍非 UTF-8 字符）
        text = p.read_text(encoding="latin-1", errors="replace")

        # 检查 1：收敛性
        # CalculiX 收敛时日志中会出现 "converged" 或 "j o b   c o m p l e t e d"
        converged = ("converged" in text.lower() or
                     "j o b   c o m p l e t e d" in text.lower())
        results.append(DiagnosticCheck(
            layer="runtime",
            check_name="ccx_convergence",
            passed=converged,
            message="Job converged." if converged else "Convergence not detected in log.",
            suggestion="" if converged else (
                "Check boundary conditions, material properties, and mesh quality. "
                "Consider enabling nlgeom or reducing load increments."
                # 建议：检查边界条件、材料参数、网格质量
                # 考虑启用 nlgeom（几何非线性）或减小载荷增量步
            ),
        ))

        # 检查 2：致命错误
        # 搜索大写 ERROR / FATAL / Segmentation（CalculiX 崩溃标志）
        fatal = any(word in text for word in ("ERROR", "FATAL", "Segmentation"))
        if fatal:
            results.append(DiagnosticCheck(
                layer="runtime",
                check_name="ccx_no_fatal_error",
                passed=False,
                message="Fatal error detected in solver log.",
                suggestion="Review ccx_run.log for the specific error message.",
            ))
        else:
            results.append(DiagnosticCheck(
                layer="runtime",
                check_name="ccx_no_fatal_error",
                passed=True,
            ))

        return results
