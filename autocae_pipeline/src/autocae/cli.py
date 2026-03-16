"""AutoCAE 命令行界面（CLI）— 流水线的用户入口。

提供子命令：
    autocae intake [--text/--step-file] — V3 检索优先入口（输出 CaseSpec + intake_decision）
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
from typing import Literal, Optional, cast

import typer
from loguru import logger
from rich import print as rprint
from rich.table import Table

# 流水线主控类
from autocae.backend.orchestrator.pipeline import PipelineRunner
from autocae.backend.intake.service import IntakeService
from autocae.backend.review.cad_gate import CadGateService
from autocae.backend.review.mesh_gate import MeshGateService
# 模板注册表（list-templates 命令使用）
from autocae.backend.templates.registry import TemplateRegistry

# 创建 Typer 应用实例（add_completion=False 禁用 shell 自动补全安装提示）
app = typer.Typer(
    name="autocae",
    help="AutoCAE Pipeline — Automated CAD/CAE Analysis System (Phase 1 MVP)",
    add_completion=False,
)
preview_app = typer.Typer(help="Stage review gate commands.")
app.add_typer(preview_app, name="preview")


@app.command()
def intake(
    text: Optional[str] = typer.Option(
        None,
        "--text",
        help="自然语言输入（例如：flat plate tension length=200 width=25 thickness=2）",
    ),
    step_file: Optional[Path] = typer.Option(
        None,
        "--step-file",
        help="STEP 文件输入路径（支持 .step/.stp）",
    ),
    image_file: Optional[Path] = typer.Option(
        None,
        "--image-file",
        help="图片输入接口预留（当前仅记录输入，不做图像语义解析）",
    ),
    runs_dir: Path = typer.Option(Path("runs"), help="Intake 输出根目录（默认 ./runs）"),
    project_case_library: Path = typer.Option(
        Path("project_case_library"),
        "--project-case-library",
        help="Project Case Library 根目录（用于检索历史 case_spec.json）",
    ),
    min_reuse_confidence: float = typer.Option(
        0.75,
        "--min-reuse-confidence",
        min=0.0,
        max=1.0,
        help="复用阈值（>= 阈值走 reuse，否则走 generate）",
    ),
    verbose: bool = typer.Option(False, help="Enable verbose logging"),
) -> None:
    """V3 Intake：先检索（Template/Project Case），再决定复用或生成。

    输出：
        runs/<case_id>/case_spec.json
        runs/<case_id>/intake_decision.json
    """
    if not verbose:
        logger.remove()
        logger.add(lambda msg: rprint(f"[dim]{msg}[/dim]"), level="INFO")

    svc = IntakeService()

    try:
        outcome = svc.intake(
            text=text,
            step_file=step_file,
            image_file=image_file,
            runs_dir=runs_dir,
            project_case_library=project_case_library,
            min_reuse_confidence=min_reuse_confidence,
        )
    except Exception as exc:
        rprint(f"[bold red]Intake failed:[/bold red] {exc}")
        raise typer.Exit(code=1)

    rprint(f"\n[bold green]Intake completed[/bold green] -> {outcome.run_dir}")
    rprint(
        f"  route: [cyan]{outcome.decision.get('final_path')}[/cyan] | "
        f"case_id: [cyan]{outcome.case_spec.metadata.case_id}[/cyan]"
    )
    rprint(f"  case_spec: {outcome.case_spec_path}")
    rprint(f"  intake_decision: {outcome.intake_decision_path}")
    if image_file is not None:
        rprint(
            "  [yellow]note:[/yellow] image intake parser is reserved; "
            "current version only records image metadata in intake_decision.json."
        )


def _resolve_gate_decision(decision: Optional[str]) -> Literal["confirm", "edit", "abort"]:
    valid = {"confirm", "edit", "abort"}
    if decision is not None:
        norm = decision.strip().lower()
        if norm in valid:
            return cast(Literal["confirm", "edit", "abort"], norm)
        raise ValueError("Invalid decision. Expected one of: confirm, edit, abort.")

    while True:
        raw = typer.prompt("CAD decision [confirm/edit/abort]", default="confirm")
        norm = raw.strip().lower()
        if norm in valid:
            return cast(Literal["confirm", "edit", "abort"], norm)
        rprint("[yellow]Invalid decision, please enter confirm/edit/abort.[/yellow]")


@preview_app.command("cad")
def preview_cad(
    run_dir: Path = typer.Argument(
        ...,
        help="run 目录路径（支持根目录结构或 02_cad 子目录结构）",
    ),
    decision: Optional[str] = typer.Option(
        None,
        "--decision",
        help="用户决策：confirm|edit|abort（不传则交互询问）",
    ),
    comment: str = typer.Option("", "--comment", help="用户备注（可选）"),
    edit_request: str = typer.Option("", "--edit-request", help="edit 时的修改请求（可选）"),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help="打开交互预览窗口（默认仅离屏生成 PNG）",
    ),
    verbose: bool = typer.Option(False, help="Enable verbose logging"),
) -> None:
    """执行 CAD Gate：auto_check -> preview -> user_confirm -> review_transcript."""
    if not verbose:
        logger.remove()
        logger.add(lambda msg: rprint(f"[dim]{msg}[/dim]"), level="INFO")

    try:
        resolved_decision = _resolve_gate_decision(decision)
    except Exception as exc:
        rprint(f"[bold red]CAD gate failed:[/bold red] {exc}")
        raise typer.Exit(code=1)

    gate = CadGateService()
    try:
        outcome = gate.run_gate(
            run_dir=run_dir,
            decision=resolved_decision,
            comment=comment,
            edit_request=edit_request,
            interactive_preview=interactive,
        )
    except Exception as exc:
        rprint(f"[bold red]CAD gate failed:[/bold red] {exc}")
        raise typer.Exit(code=1)

    table = Table(title="CAD Gate Checks", show_header=True)
    table.add_column("Check")
    table.add_column("Passed")
    table.add_column("Message")
    for check in outcome.checks:
        table.add_row(
            check.get("name", ""),
            "yes" if bool(check.get("passed")) else "no",
            check.get("message", ""),
        )

    rprint(table)
    rprint(f"decision: [cyan]{outcome.decision}[/cyan]")
    rprint(f"next_stage_allowed: [cyan]{outcome.next_stage_allowed}[/cyan]")
    if outcome.preview_png:
        rprint(f"preview_png: {outcome.preview_png}")
    if outcome.transcript_path:
        rprint(f"review_transcript: {outcome.transcript_path}")


@preview_app.command("mesh")
def preview_mesh(
    run_dir: Path = typer.Argument(
        ...,
        help="run 目录路径（支持根目录结构或 03_mesh 子目录结构）",
    ),
    decision: Optional[str] = typer.Option(
        None,
        "--decision",
        help="用户决策：confirm|edit|abort（不传则交互询问）",
    ),
    comment: str = typer.Option("", "--comment", help="用户备注（可选）"),
    edit_request: str = typer.Option("", "--edit-request", help="edit 时的修改请求（可选）"),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help="打开交互预览窗口（默认仅离屏生成 PNG）",
    ),
    verbose: bool = typer.Option(False, help="Enable verbose logging"),
) -> None:
    """执行 Mesh Gate：auto_check -> preview -> user_confirm -> review_transcript."""
    if not verbose:
        logger.remove()
        logger.add(lambda msg: rprint(f"[dim]{msg}[/dim]"), level="INFO")

    try:
        resolved_decision = _resolve_gate_decision(decision)
    except Exception as exc:
        rprint(f"[bold red]Mesh gate failed:[/bold red] {exc}")
        raise typer.Exit(code=1)

    gate = MeshGateService()
    try:
        outcome = gate.run_gate(
            run_dir=run_dir,
            decision=resolved_decision,
            comment=comment,
            edit_request=edit_request,
            interactive_preview=interactive,
        )
    except Exception as exc:
        rprint(f"[bold red]Mesh gate failed:[/bold red] {exc}")
        raise typer.Exit(code=1)

    table = Table(title="Mesh Gate Checks", show_header=True)
    table.add_column("Check")
    table.add_column("Passed")
    table.add_column("Message")
    for check in outcome.checks:
        table.add_row(
            check.get("name", ""),
            "yes" if bool(check.get("passed")) else "no",
            check.get("message", ""),
        )

    rprint(table)
    rprint(f"decision: [cyan]{outcome.decision}[/cyan]")
    rprint(f"next_stage_allowed: [cyan]{outcome.next_stage_allowed}[/cyan]")
    if outcome.preview_png:
        rprint(f"preview_png: {outcome.preview_png}")
    if outcome.transcript_path:
        rprint(f"review_transcript: {outcome.transcript_path}")


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
    cad_mode: str = typer.Option(
        "template",
        "--cad-mode",
        help="CAD stage mode: template | llm (LLM generation + bounded auto-repair).",
    ),
    cad_llm_max_attempts: int = typer.Option(
        3,
        "--cad-llm-max-attempts",
        min=1,
        help="Bounded retry max_attempts for CAD LLM generation/repair.",
    ),
    mesh_mode: str = typer.Option(
        "template",
        "--mesh-mode",
        help="Mesh stage mode: template | llm (LLM generation + bounded auto-repair).",
    ),
    mesh_llm_max_attempts: int = typer.Option(
        3,
        "--mesh-llm-max-attempts",
        min=1,
        help="Bounded retry max_attempts for Mesh LLM generation/repair.",
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
    runner = PipelineRunner(
        runs_dir=runs_dir,
        dry_run=dry_run,
        cad_mode=cad_mode,
        cad_llm_max_attempts=cad_llm_max_attempts,
        mesh_mode=mesh_mode,
        mesh_llm_max_attempts=mesh_llm_max_attempts,
    )

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
        rprint(f"\n[bold green]Pipeline completed[/bold green] -> {result.run_dir}")
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
        rprint(f"\n[bold red]Pipeline failed:[/bold red] {result.error_message}")
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
    from autocae.backend.input.loader import CaseSpecLoader
    from autocae.backend.input.validator import CaseSpecValidator

    builder = CaseSpecLoader()
    validator = CaseSpecValidator()

    try:
        # 根据扩展名选择加载方式
        spec = (builder.from_yaml(case_file) if case_file.suffix in (".yaml", ".yml")
                else builder.from_json(case_file))

        # 运行 Layer A 验证（业务规则校验）
        val_result = validator.validate(spec)

        if val_result.passed:
            rprint(f"[green]Validation passed[/green] for '{spec.metadata.case_name}'")
            # 即使通过，也显示警告信息（如参数超出推荐范围等）
            if val_result.warnings:
                for w in val_result.warnings:
                    rprint(f"  [yellow]warning: {w}[/yellow]")
        else:
            rprint(f"[red]Validation failed[/red]")
            # 逐行打印所有错误信息（如拓扑/几何类型不匹配等）
            for e in val_result.errors:
                rprint(f"  [red]- {e}[/red]")
            raise typer.Exit(code=1)

    except Exception as exc:
        # 文件读取失败或 Pydantic 解析失败（格式错误）
        rprint(f"[red]Error loading case file:[/red] {exc}")
        raise typer.Exit(code=1)


@app.command()
def solve(
    run_dir: Path = typer.Argument(
        ...,
        help="已有的 run 目录路径（runs/<case_id>/），其中须包含 solver_job.json 和 analysis_model.json",
    ),
    ccx_path: Optional[str] = typer.Option(
        None,
        "--ccx-path",
        help="CalculiX 可执行文件路径（默认：CCX_PATH 环境变量，或 PATH 中的 ccx）",
    ),
    verbose: bool = typer.Option(False, help="Enable verbose logging"),
    enforce_mesh_gate: bool = typer.Option(
        True,
        "--enforce-mesh-gate/--no-enforce-mesh-gate",
        help="是否在求解前强制检查 Mesh Gate 已通过（V3 默认开启）",
    ),
) -> None:
    """对已有 run 目录执行 CalculiX 求解并运行后处理（Stage 6 + 7）。

    适用场景：

    \\b
        # 对 dry-run 生成的目录执行真实求解
        autocae solve runs/case_3800d1c7/

    \\b
        # 指定 CalculiX 可执行文件路径
        autocae solve runs/case_3800d1c7/ --ccx-path /opt/ccx/bin/ccx

    若目录中已存在 run_status.json 且状态为 COMPLETED，则跳过求解直接运行后处理。
    """
    if not verbose:
        logger.remove()
        logger.add(lambda msg: rprint(f"[dim]{msg}[/dim]"), level="INFO")

    runner = PipelineRunner(ccx_executable=ccx_path)
    result = runner.solve_from_run_dir(run_dir, enforce_mesh_gate=enforce_mesh_gate)

    if result.success:
        rprint(f"\n[bold green]Solve completed[/bold green] -> {result.run_dir}")
        if result.result_summary:
            s = result.result_summary
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
            if s.natural_frequencies:
                table.add_row(
                    "Natural Freq (1st)", f"{s.natural_frequencies[0]:.4e}", "Hz"
                )
            rprint(table)
    else:
        rprint(f"\n[bold red]Solve failed:[/bold red] {result.error_message}")
        raise typer.Exit(code=1)


@app.command()
def visualize(
    target: Path = typer.Argument(
        ...,
        help="运行目录（runs/<case_id>/）或单个 .step / .inp 文件",
    ),
    mode: str = typer.Option(
        "auto",
        "--mode", "-m",
        help="可视化模式：auto（自动检测）| cad（仅 CAD）| mesh（仅网格）| results（仅 CalculiX 结果场）",
    ),
    no_interactive: bool = typer.Option(
        False,
        "--no-interactive",
        help="关闭交互窗口（仅保存 PNG，适合无显示器环境）",
    ),
    no_save: bool = typer.Option(
        False,
        "--no-save",
        help="不保存 PNG 截图",
    ),
    groups_json: Optional[Path] = typer.Option(
        None,
        "--groups",
        help="mesh_groups.json 路径（mode=mesh 时使用，默认从同目录自动查找）",
    ),
) -> None:
    """可视化 CAD 几何（STEP）、有限元网格（mesh.inp）和 CalculiX 结果场（job.frd）。

    用法示例：

    \\b
        # 可视化整个运行目录（CAD + 网格 + 位移/应力结果场，交互模式）
        autocae visualize runs/case_001/

    \\b
        # 仅可视化 CAD 几何，保存 PNG 后退出
        autocae visualize runs/case_001/model.step --mode cad --no-interactive

    \\b
        # 仅可视化网格，无显示器模式
        autocae visualize runs/case_001/mesh.inp --mode mesh --no-interactive

    \\b
        # CI 批处理：关闭交互窗口，只保存截图（含 CAD + 网格 + 结果场共 4 张 PNG）
        autocae visualize runs/case_001/ --no-interactive
    """
    from autocae.backend.services.visualization_service import VisualizationService

    svc = VisualizationService()
    interactive = not no_interactive
    save_png = not no_save
    target = Path(target)

    try:
        # ── 自动检测：单文件 vs 目录 ──────────────────────────────────────
        if target.is_dir():
            rprint(f"[bold cyan]Visualizing run directory:[/bold cyan] {target}")
            results = svc.visualize_run(
                run_dir=target,
                interactive=interactive,
                save_png=save_png,
            )
            for key, png in results.items():
                if png:
                    rprint(f"  [green]ok[/green] {key} screenshot -> {png}")

        elif target.suffix.lower() in (".step", ".stp") or mode == "cad":
            # 单 STEP 文件
            step_file = target
            output_dir = target.parent if save_png else None
            # 尝试从同目录读取 geometry_meta.json 获取 bounding_box
            bbox: dict = {}
            meta_path = target.parent / "geometry_meta.json"
            if meta_path.exists():
                import json
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                bbox = meta.get("bounding_box") or {}
            rprint(f"[bold cyan]Visualizing CAD geometry:[/bold cyan] {step_file}")
            png = svc.visualize_cad(
                step_file=step_file,
                bounding_box=bbox,
                output_dir=output_dir,
                interactive=interactive,
                save_png=save_png,
            )
            if png:
                rprint(f"  [green]ok[/green] Screenshot saved -> {png}")

        elif target.suffix.lower() == ".inp" or mode == "mesh":
            # 单 INP 文件
            mesh_file = target
            output_dir = target.parent if save_png else None
            # 自动查找 mesh_groups.json
            resolved_groups = groups_json or (
                target.parent / "mesh_groups.json"
                if (target.parent / "mesh_groups.json").exists()
                else None
            )
            rprint(f"[bold cyan]Visualizing FE mesh:[/bold cyan] {mesh_file}")
            png = svc.visualize_mesh(
                mesh_inp_file=mesh_file,
                groups_json=resolved_groups,
                output_dir=output_dir,
                interactive=interactive,
                save_png=save_png,
            )
            if png:
                rprint(f"  [green]ok[/green] Screenshot saved -> {png}")

        else:
            rprint(f"[red]Cannot determine visualization mode for:[/red] {target}")
            rprint("  Use --mode cad | mesh, or pass a run directory / .step / .inp file.")
            raise typer.Exit(code=1)

    except Exception as exc:
        rprint(f"[bold red]Visualization error:[/bold red] {exc}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    # 允许直接 `python cli.py` 调用（通常通过 `autocae` 命令入口调用）
    app()
