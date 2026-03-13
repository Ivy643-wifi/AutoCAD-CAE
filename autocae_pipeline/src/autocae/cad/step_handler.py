"""外部 STEP 文件处理器（G-02 双轨制备轨）。

当用户直接提供 STEP 文件时，跳过 CadQuery 参数化建模阶段，
直接将外部 STEP 文件纳入流水线，并提取必要的几何元信息。

设计原则：
    G-02：CAD 双轨制 — CadQuery 主轨 / 外部 STEP 备轨（本模块实现备轨）
    G-03：几何交换格式唯一为 STEP
    G-11：文件接口驱动 — 将外部 STEP 复制到 run 目录，保持 run 目录自包含

使用方式（由 PipelineRunner 在 Stage 2 调用）：
    handler = ExternalStepHandler()
    cad_result = handler.build(step_path=Path("model.step"), output_dir=run_dir)
"""

from __future__ import annotations

import shutil
from pathlib import Path

from loguru import logger

from autocae.cad.templates.base import CADResult
from autocae.schemas.mesh import GeometryMeta, GeometrySource

# Gmsh 支持的 STEP 文件扩展名（不区分大小写）
_STEP_SUFFIXES = {".step", ".stp"}


class ExternalStepHandler:
    """将外部 STEP 文件纳入流水线，生成与 CadQuery 路径兼容的 CADResult。

    主要职责：
        1. 校验外部 STEP 文件存在且格式正确
        2. 将文件复制到 run 目录（model.step），保持 G-11 文件接口规范
        3. 通过 Gmsh 读取 STEP 并提取包围盒（MeshBuilder 位置启发式分组所需）
        4. 构造 GeometryMeta（source=EXTERNAL_STEP）并写出 geometry_meta.json
        5. 返回 CADResult（与 CadQuery 路径接口完全兼容）
    """

    def build(self, step_path: Path, output_dir: Path) -> CADResult:
        """处理外部 STEP 文件，返回与 CADBuilder.build() 兼容的 CADResult。

        Args:
            step_path:  用户提供的 STEP 文件路径（.step 或 .stp）
            output_dir: 本次运行目录（model.step 和 geometry_meta.json 写入此处）

        Returns:
            CADResult（step_file 指向 output_dir/model.step，geometry_meta 含包围盒）

        Raises:
            FileNotFoundError: step_path 文件不存在
            ValueError:        文件扩展名不是 .step/.stp
            RuntimeError:      Gmsh 导入 STEP 失败（文件损坏或格式不兼容）
        """
        step_path = Path(step_path)
        output_dir = Path(output_dir)

        # ------------------------------------------------------------------
        # Step 1: 输入校验
        # ------------------------------------------------------------------
        if not step_path.exists():
            raise FileNotFoundError(f"External STEP file not found: {step_path}")
        if step_path.suffix.lower() not in _STEP_SUFFIXES:
            raise ValueError(
                f"Unsupported file extension '{step_path.suffix}'. "
                f"Expected one of: {sorted(_STEP_SUFFIXES)}"
            )

        logger.info(f"ExternalStepHandler → source STEP: {step_path}")

        # ------------------------------------------------------------------
        # Step 2: 复制 STEP 到 run 目录（G-11：run 目录自包含）
        # ------------------------------------------------------------------
        dest_step = output_dir / "model.step"
        if step_path.resolve() != dest_step.resolve():
            shutil.copy2(step_path, dest_step)
            logger.info(f"STEP copied → {dest_step}")
        else:
            logger.info(f"STEP already at destination: {dest_step}")

        # ------------------------------------------------------------------
        # Step 3: 通过 Gmsh 提取包围盒
        # ------------------------------------------------------------------
        bounding_box = self._extract_bounding_box(dest_step)

        # ------------------------------------------------------------------
        # Step 4: 构造 GeometryMeta 并写出 geometry_meta.json
        # ------------------------------------------------------------------
        geometry_meta = GeometryMeta(
            step_file=str(dest_step),
            source=GeometrySource.EXTERNAL_STEP,
            bounding_box=bounding_box,
            # named_faces / named_edges 留空，由 MeshBuilder 通过位置启发式填充
        )

        meta_path = output_dir / "geometry_meta.json"
        meta_path.write_text(geometry_meta.to_json(), encoding="utf-8")
        logger.info(f"geometry_meta.json saved → {meta_path}")

        return CADResult(
            step_file=dest_step,
            geometry_meta=geometry_meta,
        )

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_bounding_box(step_path: Path) -> dict[str, float]:
        """使用 Gmsh 导入 STEP 文件并提取模型包围盒。

        包围盒格式与 MeshBuilder 期望的 bounding_box 字段一致：
            {"xmin": ..., "xmax": ..., "ymin": ..., "ymax": ..., "zmin": ..., "zmax": ...}

        Gmsh 会话在函数结束后关闭（不影响外部 Gmsh 状态）。

        Raises:
            RuntimeError: Gmsh 初始化失败或 STEP 导入失败
        """
        try:
            import gmsh  # 延迟导入，与 MeshBuilder 保持一致
        except ImportError as exc:
            raise RuntimeError(
                "gmsh is required for ExternalStepHandler but is not installed. "
                "Run: pip install gmsh"
            ) from exc

        gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Verbosity", 0)  # 静默模式，减少日志噪声
            gmsh.model.add("step_bbox_probe")
            gmsh.model.occ.importShapes(str(step_path))
            gmsh.model.occ.synchronize()

            # getBoundingBox(-1, -1) → 整个模型的包围盒
            xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
            logger.debug(
                f"Bounding box: x=[{xmin:.3f}, {xmax:.3f}] "
                f"y=[{ymin:.3f}, {ymax:.3f}] z=[{zmin:.3f}, {zmax:.3f}]"
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to import STEP file '{step_path}' via Gmsh: {exc}"
            ) from exc
        finally:
            gmsh.finalize()

        return {
            "xmin": xmin, "xmax": xmax,
            "ymin": ymin, "ymax": ymax,
            "zmin": zmin, "zmax": zmax,
        }
