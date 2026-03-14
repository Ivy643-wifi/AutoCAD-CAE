"""CAD service — 几何生成服务（Stage 2）。

职责：
  - 主轨：根据 GeometryType 分派到对应 CAD 模板，生成 model.step
  - 备轨（G-02）：接受外部 STEP 文件，跳过 CadQuery 建模
"""

from __future__ import annotations

import shutil
from pathlib import Path

from loguru import logger

from autocae.backend.templates.cad.base import CADResult
from autocae.backend.templates.cad.flat_plate import FlatPlateTemplate
from autocae.backend.templates.cad.open_hole_plate import OpenHolePlateTemplate
from autocae.backend.templates.cad.cylindrical_shell import CylindricalShellTemplate
from autocae.backend.templates.cad.laminated_beam import LaminatedBeamTemplate
from autocae.backend.templates.cad.stringer_stiffened_panel import StringerStiffenedPanelTemplate
from autocae.backend.templates.cad.sandwich_plate import SandwichPlateTemplate
from autocae.backend.templates.cad.bolted_lap_joint import BoltedLapJointTemplate
from autocae.schemas.case_spec import CaseSpec, GeometryType
from autocae.schemas.mesh import GeometryMeta, GeometrySource


# ---------------------------------------------------------------------------
# CAD 模板注册表：GeometryType → 模板实例（单例复用）
# ---------------------------------------------------------------------------
_TEMPLATE_REGISTRY = {
    GeometryType.FLAT_PLATE:               FlatPlateTemplate(),
    GeometryType.OPEN_HOLE_PLATE:          OpenHolePlateTemplate(),
    GeometryType.NOTCHED_PLATE:            FlatPlateTemplate(),          # 缺口板复用平板
    GeometryType.CYLINDRICAL_SHELL:        CylindricalShellTemplate(),
    GeometryType.PRESSURE_SHELL:           CylindricalShellTemplate(),   # 复用圆柱壳
    GeometryType.LAMINATED_BEAM:           LaminatedBeamTemplate(),
    GeometryType.STRINGER_STIFFENED_PANEL: StringerStiffenedPanelTemplate(),
    GeometryType.SANDWICH_PLATE:           SandwichPlateTemplate(),
    GeometryType.BOLTED_LAP_JOINT:         BoltedLapJointTemplate(),
}

_STEP_SUFFIXES = {".step", ".stp"}


class CADService:
    """Stage 2 service: generate STEP geometry from CaseSpec.

    - build()            — CadQuery 主轨（G-02 primary track）
    - build_from_step()  — External STEP 备轨（G-02 fallback track）
    """

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        """查找对应模板并生成 STEP + geometry_meta.json（主轨）。"""
        geo_type = spec.geometry.geometry_type
        template = _TEMPLATE_REGISTRY.get(geo_type)
        if template is None:
            raise KeyError(
                f"No CAD template registered for geometry type '{geo_type}'. "
                f"Available: {list(_TEMPLATE_REGISTRY.keys())}"
            )

        logger.info(f"CADService → template: {template.__class__.__name__}")
        result = template.build(spec, output_dir)

        meta_path = output_dir / "geometry_meta.json"
        meta_path.write_text(result.geometry_meta.to_json(), encoding="utf-8")
        logger.info(f"geometry_meta.json saved → {meta_path}")
        return result

    def build_from_step(self, step_path: Path, output_dir: Path) -> CADResult:
        """将外部 STEP 文件纳入流水线，生成兼容的 CADResult（备轨）。"""
        step_path = Path(step_path)
        output_dir = Path(output_dir)

        if not step_path.exists():
            raise FileNotFoundError(f"External STEP file not found: {step_path}")
        if step_path.suffix.lower() not in _STEP_SUFFIXES:
            raise ValueError(
                f"Unsupported file extension '{step_path.suffix}'. "
                f"Expected one of: {sorted(_STEP_SUFFIXES)}"
            )

        logger.info(f"CADService (external STEP) → source: {step_path}")
        dest_step = output_dir / "model.step"
        if step_path.resolve() != dest_step.resolve():
            shutil.copy2(step_path, dest_step)
            logger.info(f"STEP copied → {dest_step}")

        bounding_box = self._extract_bounding_box(dest_step)

        geometry_meta = GeometryMeta(
            step_file=str(dest_step),
            source=GeometrySource.EXTERNAL_STEP,
            bounding_box=bounding_box,
        )

        meta_path = output_dir / "geometry_meta.json"
        meta_path.write_text(geometry_meta.to_json(), encoding="utf-8")
        logger.info(f"geometry_meta.json saved → {meta_path}")

        return CADResult(step_file=dest_step, geometry_meta=geometry_meta)

    @staticmethod
    def _extract_bounding_box(step_path: Path) -> dict:
        """Use Gmsh to extract bounding box from STEP file."""
        try:
            import gmsh
        except ImportError as exc:
            raise RuntimeError("gmsh is required for external STEP handling.") from exc

        gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Verbosity", 0)
            gmsh.model.add("step_bbox_probe")
            gmsh.model.occ.importShapes(str(step_path))
            gmsh.model.occ.synchronize()
            xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
        except Exception as exc:
            raise RuntimeError(f"Failed to import STEP '{step_path}' via Gmsh: {exc}") from exc
        finally:
            gmsh.finalize()

        return {
            "xmin": xmin, "xmax": xmax,
            "ymin": ymin, "ymax": ymax,
            "zmin": zmin, "zmax": zmax,
        }

    @staticmethod
    def list_supported_geometry_types() -> list[str]:
        return [gt.value for gt in _TEMPLATE_REGISTRY]
