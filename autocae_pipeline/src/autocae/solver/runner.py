"""Solver Runner — 执行求解器进程并跟踪运行状态。

职责：
    接收 SolverJob 描述符，调用系统命令启动 CalculiX（或其他求解器），
    等待完成（或超时中止），收集结果文件路径，写入 run_status.json。

CalculiX 的调用方式：
    命令行：ccx <job_name>（不含扩展名，CalculiX 自动寻找 <job_name>.inp）
    输出文件：<job_name>.frd（场结果）, <job_name>.dat（文本摘要）, <job_name>.cvg（收敛历程）
    日志：所有 stdout/stderr 重定向到 ccx_run.log

注意：
    - dry_run 模式下，PipelineRunner 不会调用此模块，而是直接创建一个假的 RunStatus。
    - ccx 必须在 PATH 中可找到，否则抛出 FileNotFoundError（以友好错误信息包装）。
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

from autocae.schemas.solver import RunStatus, RunStatusEnum, SolverJob, SolverType


# 求解器类型 → 可执行程序名称的映射
# 扩展：添加新求解器只需在此字典中添加条目
_SOLVER_EXECUTABLES: dict[SolverType, str] = {
    SolverType.CALCULIX: "ccx",  # CalculiX 可执行程序名（需在系统 PATH 中）
}


class SolverRunner:
    """执行求解任务并追踪其运行状态。

    使用方式：
        runner = SolverRunner()
        run_status = runner.run(solver_job)
        # run_status.status == RunStatusEnum.COMPLETED → 有结果可解析
    """

    def run(self, job: SolverJob) -> RunStatus:
        """启动求解器并等待完成，返回 RunStatus。

        流程：
            1. 查找求解器可执行程序名
            2. 构造命令行（ccx <job_name>）
            3. 启动子进程，将输出重定向到 ccx_run.log
            4. 等待完成（有超时限制）
            5. 检查返回码，收集结果文件
            6. 写入 run_status.json

        Args:
            job: SolverJob（由 CalculiXAdapter.build_solver_job() 创建）

        Returns:
            RunStatus（含状态、耗时、结果文件路径）
        """
        # 初始化 RunStatus 为"运行中"
        status = RunStatus(job_id=job.job_id, status=RunStatusEnum.RUNNING)
        status.start_time = datetime.utcnow()

        executable = _SOLVER_EXECUTABLES.get(job.solver_type)
        if executable is None:
            status.status = RunStatusEnum.FAILED
            status.error_message = f"No executable registered for solver type '{job.solver_type}'"
            return status

        work_dir = Path(job.working_dir)
        inp_file  = Path(job.input_files[0])
        job_name  = inp_file.stem   # e.g. "job.inp" → "job"
        log_path  = work_dir / "ccx_run.log"

        # 构造命令：ccx job（CalculiX 约定不带扩展名）
        cmd = [executable, job_name]
        if job.profile.threads > 1:
            # 多线程：通过 OMP_NUM_THREADS 环境变量控制 OpenMP 线程数
            cmd = [f"OMP_NUM_THREADS={job.profile.threads}", *cmd]

        logger.info(f"SolverRunner: executing '{' '.join(cmd)}' in {work_dir}")
        t0 = time.perf_counter()

        try:
            # 打开日志文件，将 stdout 和 stderr 都重定向进去
            with open(log_path, "w", encoding="utf-8") as log_fh:
                proc = subprocess.run(
                    cmd,
                    cwd=str(work_dir),        # 工作目录设为运行目录
                    stdout=log_fh,            # 标准输出 → 日志文件
                    stderr=subprocess.STDOUT, # 标准错误 → 同一日志文件
                    timeout=job.resource_limits.max_wall_time_s,  # 超时限制
                )
            return_code = proc.returncode

        except FileNotFoundError:
            # ccx 不在 PATH 中
            status.status = RunStatusEnum.FAILED
            status.error_message = (
                f"Solver executable '{executable}' not found. "
                "Please ensure CalculiX (ccx) is installed and on PATH."
            )
            status.end_time = datetime.utcnow()
            return status

        except subprocess.TimeoutExpired:
            # 超过 max_wall_time_s 秒仍未完成
            status.status = RunStatusEnum.ABORTED
            status.error_message = "Solver exceeded maximum wall time."
            status.end_time = datetime.utcnow()
            return status

        elapsed = time.perf_counter() - t0
        status.end_time = datetime.utcnow()
        status.wall_time_s = elapsed
        status.return_code = return_code
        status.log_file = str(log_path)

        # 收集结果文件（CalculiX 生成 .frd .dat .cvg，文件名与输入文件同名）
        result_files: list[str] = []
        for ext in (".frd", ".dat", ".cvg"):
            rfile = work_dir / f"{job_name}{ext}"
            if rfile.exists():
                result_files.append(str(rfile))

        status.result_files = result_files

        # 判断是否成功：返回码 0 且存在结果文件
        if return_code == 0 and result_files:
            status.status = RunStatusEnum.COMPLETED
            logger.info(
                f"Solver completed successfully in {elapsed:.1f}s. "
                f"Result files: {result_files}"
            )
        else:
            status.status = RunStatusEnum.FAILED
            status.failure_stage = "solver_execution"
            status.error_message = f"Solver returned non-zero exit code: {return_code}"
            logger.error(status.error_message)

        # 写入 run_status.json（PostprocessEngine 读取此文件判断是否有结果可解析）
        status_path = work_dir / "run_status.json"
        status_path.write_text(status.to_json(), encoding="utf-8")
        logger.info(f"run_status.json saved → {status_path}")

        return status
