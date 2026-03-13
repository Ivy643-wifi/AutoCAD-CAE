"""AutoCAE 命令行界面（CLI）— 流水线的用户入口。

提供三个子命令：
    autocae run <case_file>         — 运行完整流水线
    autocae validate <case_file>    — 仅校验 CaseSpec，不运行求解器
    autocae list-templates          — 列出所有已注册的 Phase 1 模板

技术栈：
    - Typer  ：命令行参数解析（基于 Python 类型注解，自动生成帮助文档）
    - Rich   ：终端富文本渲染（彩色输出、表格展示）
    - Loguru ：结构化日志（verbose 模式下输出详细日志）

使用示例：
    # 运行平板拉伸算例（dry_run 跳过实际 CCX 求解）
    autocae run examples/flat_plate_tension.yaml --dry-run

    # 仅校验 CaseSpec 合法性
    autocae validate examples/flat_plate_tension.yaml

    # 查看已注册的模板列表
    autocae list-templates
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from loguru import logger
from rich import print as rprint
from rich.table import Table

# 流水线主控类
from autocae.pipeline.runner import PipelineRunner
# 模板注册表（list-templates 命令使用）
from autocae.template_library.registry import TemplateRegistry

# 创建 Typer 应用实例（add_completion=False 禁用 shell 自动补全安装提示）
app = typer.Typer(
    name="autocae",
    help="AutoCAE Pipeline — Automated CAD/CAE Analysis System (Phase 1 MVP)",
    add_completion=False,
)


@app.command()
def run(
    case_file: Path = typer.Argument(..., help="Path to case_spec.yaml or case_spec.json"),
    runs_dir: Path = typer.Option(Path("runs"), help="Base directory for run outputs"),
    dry_run: bool = typer.Option(False, help="Skip actual solver execution"),
    verbose: bool = typer.Option(False, help="Enable verbose logging"),
    step_file: Optional[Path] = typer.Option(
        None,
        "--step-file",
        help="External STEP file path. When provided, skips CAD generation (Stage 2) "
             "and uses this STEP file directly (G-02 dual-track).",
    ),
) -> None:
    """运行完整的 AutoCAE 流水线（8 个阶段）。

    参数说明：
        case_file:  CaseSpec 文件路径（.yaml 或 .json 均可）
        runs_dir:   运行输出根目录（默认 ./runs/）
        dry_run:    跳过实际 CCX 求解（调试模式，默认 False）
        verbose:    输出详细日志（默认 False，只显示 INFO 级别）
        step_file:  外部 STEP 文件路径（可选）。提供后跳过 CadQuery 建模，
                    直接使用该 STEP 文件（G-02 双轨制备轨）。

    成功时：
        终端打印绿色成功信息 + 关键结果表格（位移、应力、屈曲因子）
    失败时：
        终端打印红色错误信息，并以退出码 1 退出（CI/CD 友好）
    """
    # 非 verbose 模式：用 Rich 渲染 loguru 输出，减少噪声
    if not verbose:
        logger.remove()
        logger.add(lambda msg: rprint(f"[dim]{msg}[/dim]"), level="INFO")

    # 初始化流水线（dry_run 控制是否跳过 CCX 执行）
    runner = PipelineRunner(runs_dir=runs_dir, dry_run=dry_run)

    # 根据是否提供外部 STEP 文件，选择对应入口方法
    if step_file is not None:
        if case_file.suffix in (".yaml", ".yml"):
            result = runner.run_from_yaml_with_step(case_file, step_file)
        else:
            result = runner.run_from_json_with_step(case_file, step_file)
    elif case_file.suffix in (".yaml", ".yml"):
        result = runner.run_from_yaml(case_file)
    else:
        result = runner.run_from_json(case_file)

    if result.success:
        # 成功 → 打印结果目录路径 + 关键结果表格
        rprint(f"\n[bold green]✓ Pipeline completed[/bold green] → {result.run_dir}")
        if result.result_summary:
            s = result.result_summary
            # 用 Rich Table 展示关键标量结果
            table = Table(title="Result Summary", show_header=True)
            table.add_column("Metric")
            table.add_column("Value")
            table.add_column("Unit")
            if s.max_displacement is not None:
                table.add_row("Max Displacement", f"{s.max_displacement:.4e}", "mm")
            if s.max_mises_stress is not None:
                table.add_row("Max Mises Stress", f"{s.max_mises_stress:.4e}", "MPa")
            if s.buckling_load_factor is not None:
                table.add_row("Buckling Load Factor", f"{s.buckling_load_factor:.4f}", "—")
            rprint(table)
    else:
        # 失败 → 打印错误信息，以退出码 1 退出（供 CI/CD 脚本检测）
        rprint(f"\n[bold red]✗ Pipeline failed:[/bold red] {result.error_message}")
        raise typer.Exit(code=1)


@app.command()
def list_templates() -> None:
    """列出所有已注册的 Phase 1 算例模板。

    输出格式：表格，包含列：
        Template ID   — 模板唯一标识符（如 flat_plate_tension_v1）
        Geometry Type — 几何类型（如 flat_plate, cylindrical_shell）
        Analysis Type — 分析类型（如 static_tension, buckling）
        Version       — 模板版本号（如 1.0）
    """
    registry = TemplateRegistry()
    table = Table(title="Registered Templates", show_header=True)
    table.add_column("Template ID")
    table.add_column("Geometry Type")
    table.add_column("Analysis Type")
    table.add_column("Version")

    # 遍历所有已注册模板，逐行添加到表格
    for tid in registry.list_templates():
        t = registry.get(tid)
        if t:
            table.add_row(t.template_id, t.geometry_type.value, t.analysis_type.value, t.version)

    rprint(table)


@app.command()
def validate(
    case_file: Path = typer.Argument(..., help="Path to case_spec.yaml or case_spec.json"),
) -> None:
    """仅校验 CaseSpec 文件的合法性，不运行流水线。

    用途：
        在提交到批量计算队列之前，快速检查 CaseSpec 是否合法，
        避免因输入错误导致求解器崩溃。

    参数：
        case_file: CaseSpec 文件路径（.yaml 或 .json 均可）

    成功时（所有检查通过）：
        打印绿色 ✓ 信息；若有警告，以黄色 ⚠ 列出

    失败时（有错误）：
        打印红色 ✗ 错误列表，以退出码 1 退出
    """
    # 延迟导入，避免每次 CLI 启动时都加载所有模块
    from autocae.case_spec.builder import CaseSpecBuilder
    from autocae.case_spec.validator import CaseSpecValidator

    builder = CaseSpecBuilder()
    validator = CaseSpecValidator()

    try:
        # 根据扩展名选择加载方式
        spec = (builder.from_yaml(case_file) if case_file.suffix in (".yaml", ".yml")
                else builder.from_json(case_file))

        # 运行 Layer A 验证（业务规则校验）
        val_result = validator.validate(spec)

        if val_result.passed:
            rprint(f"[green]✓ Validation passed[/green] for '{spec.metadata.case_name}'")
            # 即使通过，也显示警告信息（如参数超出推荐范围等）
            if val_result.warnings:
                for w in val_result.warnings:
                    rprint(f"  [yellow]⚠ {w}[/yellow]")
        else:
            rprint(f"[red]✗ Validation failed[/red]")
            # 逐行打印所有错误信息（如拓扑/几何类型不匹配等）
            for e in val_result.errors:
                rprint(f"  [red]• {e}[/red]")
            raise typer.Exit(code=1)

    except Exception as exc:
        # 文件读取失败或 Pydantic 解析失败（格式错误）
        rprint(f"[red]Error loading case file:[/red] {exc}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    # 允许直接 `python cli.py` 调用（通常通过 `autocae` 命令入口调用）
    app()
