"""Open-hole plate CAD template.

Supported analysis types: tension, compression.

Geometry:
  - Rectangular plate with a centred through-hole.
  - Hole diameter is required in geometry.extra['hole_diameter'].
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.cad.templates.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class OpenHolePlateTemplate(BaseCADTemplate):
    """Flat plate with a centred circular through-hole (standard coupon)."""

    geometry_type = "open_hole_plate"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        import cadquery as cq

        geo = spec.geometry
        L = geo.length
        W = geo.width
        T = geo.thickness
        d = geo.extra.get("hole_diameter", W * 0.2)

        if d >= W:
            raise ValueError(
                f"hole_diameter ({d} mm) must be smaller than plate width ({W} mm)."
            )

        logger.info(f"Building open_hole_plate: L={L} W={W} T={T} hole_d={d} mm")

        plate = (
            cq.Workplane("XY")
            .box(L, W, T, centered=(True, True, True))
            .faces(">Z")
            .workplane()
            .hole(d)
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        cq.exporters.export(plate, str(step_path))
        logger.info(f"  STEP exported → {step_path}")

        geometry_meta = GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces={},
            named_edges={},
            bounding_box={
                "xmin": -L / 2, "xmax": L / 2,
                "ymin": -W / 2, "ymax": W / 2,
                "zmin": -T / 2, "zmax": T / 2,
            },
            volume=L * W * T - 3.14159 * (d / 2) ** 2 * T,
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "FIXED_END":   "Face at X = -L/2",
                "LOAD_END":    "Face at X = +L/2",
                "HOLE_SURFACE": "Cylindrical surface of the hole",
            },
        )
