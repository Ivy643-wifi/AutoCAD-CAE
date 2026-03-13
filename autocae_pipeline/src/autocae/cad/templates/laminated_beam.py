"""Laminated beam CAD template.

Supported analysis types: bending, torsion, modal.

Geometry:
  - Solid rectangular cross-section beam.
  - Length (X), width (Y), thickness (Z).
  - Named faces:
      FIXED_END   – X = -L/2 (clamped)
      FREE_END    – X = +L/2 (load application for cantilever)
      TOP_FLANGE  – Z = +T/2
      BOTTOM_FLANGE – Z = -T/2
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.cad.templates.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class LaminatedBeamTemplate(BaseCADTemplate):
    """Solid rectangular cross-section laminated beam."""

    geometry_type = "laminated_beam"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        import cadquery as cq

        geo = spec.geometry
        L = geo.length
        W = geo.width
        T = geo.thickness

        logger.info(f"Building laminated_beam: L={L} W={W} T={T} mm")

        beam = (
            cq.Workplane("YZ")
            .rect(W, T)
            .extrude(L)
            .translate((-L / 2.0, 0, 0))
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        cq.exporters.export(beam, str(step_path))
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
            volume=L * W * T,
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "FIXED_END":     "Cross-section face at X = -L/2",
                "FREE_END":      "Cross-section face at X = +L/2",
                "TOP_FLANGE":    "Face at Z = +T/2",
                "BOTTOM_FLANGE": "Face at Z = -T/2",
            },
        )
