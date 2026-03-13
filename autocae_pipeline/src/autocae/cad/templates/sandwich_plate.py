"""Sandwich plate CAD template.

Supported analysis types: bending, shear, buckling.

Geometry:
  - Two face-sheets (top + bottom) sandwiching a foam/honeycomb core.
  - geometry.extra keys:
      core_thickness    – core thickness [mm] (default = thickness * 8)
      facesheet_thickness – face sheet thickness [mm] (default = thickness)
  - geometry.thickness is used as facesheet_thickness if not in extra.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.cad.templates.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class SandwichPlateTemplate(BaseCADTemplate):
    """Sandwich plate: two face-sheets + monolithic core."""

    geometry_type = "sandwich_plate"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        import cadquery as cq

        geo = spec.geometry
        L  = geo.length
        W  = geo.width
        Tf = geo.extra.get("facesheet_thickness", geo.thickness)
        Tc = geo.extra.get("core_thickness", geo.thickness * 8.0)

        total_T = 2 * Tf + Tc

        logger.info(
            f"Building sandwich_plate: L={L} W={W} "
            f"Tf={Tf} Tc={Tc} total_T={total_T} mm"
        )

        # Bottom face-sheet
        bottom = cq.Workplane("XY").box(L, W, Tf, centered=(True, True, False))

        # Core (sits on top of bottom face-sheet)
        core = (
            cq.Workplane("XY")
            .box(L, W, Tc, centered=(True, True, False))
            .translate((0.0, 0.0, Tf))
        )

        # Top face-sheet
        top = (
            cq.Workplane("XY")
            .box(L, W, Tf, centered=(True, True, False))
            .translate((0.0, 0.0, Tf + Tc))
        )

        # Union all three (as a single solid for STEP export;
        # material assignment is handled at analysis model level via sections/regions)
        panel = bottom.union(core).union(top)

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        cq.exporters.export(panel, str(step_path))
        logger.info(f"  STEP exported → {step_path}")

        geometry_meta = GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces={},
            named_edges={},
            bounding_box={
                "xmin": -L / 2, "xmax": L / 2,
                "ymin": -W / 2, "ymax": W / 2,
                "zmin": 0.0,    "zmax": total_T,
            },
            volume=L * W * total_T,
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "FIXED_END":   "Cross-section at X = -L/2",
                "LOAD_END":    "Cross-section at X = +L/2",
                "TOP_FACE":    "Top face of upper face-sheet",
                "BOTTOM_FACE": "Bottom face of lower face-sheet",
            },
        )
