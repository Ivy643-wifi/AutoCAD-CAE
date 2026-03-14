"""完整可视化演示脚本 — runs/case_3800d1c7

对已完成的平板拉伸算例依次输出：
  1. CAD 几何  → viz_cad.png
  2. FE 网格   → viz_mesh.png  +  viz_mesh_quality.png
  3. 位移场    → viz_results_displacement.png
  4. Mises 应力 → viz_results_stress.png

使用方法（在 autocae_pipeline/ 目录下运行）：
    python scripts/run_full_visualization.py

需要安装：cadquery, gmsh, pyvista, matplotlib
"""

from __future__ import annotations

import sys
from pathlib import Path

# 使 src/ 可被直接导入（不依赖 pip install -e）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from loguru import logger
from rich import print as rprint
from rich.table import Table

from autocae.backend.services.visualization_service import VisualizationService

# ── 运行目录 ─────────────────────────────────────────────────────────────────
RUN_DIR = Path(__file__).resolve().parent.parent / "runs" / "case_3800d1c7"


def main() -> None:
    if not RUN_DIR.exists():
        rprint(f"[red]Run directory not found:[/red] {RUN_DIR}")
        sys.exit(1)

    rprint(f"\n[bold cyan]AutoCAE — Full Visualization[/bold cyan]")
    rprint(f"Run directory: [dim]{RUN_DIR}[/dim]\n")

    svc = VisualizationService()

    # 关闭交互窗口，仅保存 PNG（无显示器环境下安全运行）
    results = svc.visualize_run(
        run_dir=RUN_DIR,
        interactive=False,
        save_png=True,
    )

    # 结果汇总表
    table = Table(title="Visualization Output", show_header=True)
    table.add_column("Channel", style="bold")
    table.add_column("Output File")
    table.add_column("Status")

    labels = {
        "cad":                   "CAD Geometry (STEP)",
        "mesh":                  "FE Mesh (mesh.inp)",
        "results_displacement":  "Displacement Field (DISP)",
        "results_stress":        "von Mises Stress Field (STRESS)",
    }

    for key, label in labels.items():
        path = results.get(key)
        if path:
            table.add_row(label, str(path.name), "[green]OK saved[/green]")
        else:
            table.add_row(label, "—", "[yellow]skipped[/yellow]")

    rprint(table)
    rprint("\n[bold green]Done.[/bold green] Check the run directory for PNG files.")


if __name__ == "__main__":
    main()
