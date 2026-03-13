"""Cylindrical shell CAD template.

Supported analysis types: pressure, buckling, modal.

Geometry:
  - Circular hollow cylinder: length (axis=X), radius, wall_thickness.
  - geometry.extra keys:
      radius        – mid-plane radius [mm]  (default = width/2)
      wall_thickness – shell wall thickness [mm] (uses geometry.thickness if absent)
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.cad.templates.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class CylindricalShellTemplate(BaseCADTemplate):
    """Thin-walled cylindrical shell."""

    geometry_type = "cylindrical_shell"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        import cadquery as cq
        import math

        geo = spec.geometry
        L  = geo.length
        R  = geo.extra.get("radius", geo.width / 2.0)
        t  = geo.extra.get("wall_thickness", geo.thickness)

        logger.info(f"Building cylindrical_shell: L={L} R={R} t={t} mm")

        if t >= R:
            raise ValueError(
                f"wall_thickness ({t} mm) must be less than radius ({R} mm)."
            )

        # Build as a hollow cylinder (outer - inner)
        outer_r = R + t / 2.0
        inner_r = R - t / 2.0

        shell = (
            cq.Workplane("YZ")
            .circle(outer_r)
            .circle(inner_r)
            .extrude(L)
            .translate((-L / 2.0, 0, 0))   # centre along X
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        cq.exporters.export(shell, str(step_path))
        logger.info(f"  STEP exported → {step_path}")

        geometry_meta = GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces={},
            named_edges={},
            bounding_box={
                "xmin": -L / 2, "xmax": L / 2,
                "ymin": -outer_r, "ymax": outer_r,
                "zmin": -outer_r, "zmax": outer_r,
            },
            volume=math.pi * (outer_r ** 2 - inner_r ** 2) * L,
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "END_A":       "Annular face at X = -L/2",
                "END_B":       "Annular face at X = +L/2",
                "OUTER_SURFACE": "Outer cylindrical surface",
                "INNER_SURFACE": "Inner cylindrical surface",
            },
        )
