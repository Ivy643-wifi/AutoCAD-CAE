"""Stringer-stiffened panel CAD template.

Supported analysis types: buckling, tension, compression.

Geometry:
  - Flat skin plate + N equally-spaced T-section or rectangular stringers.
  - geometry.extra keys:
      n_stringers      – number of stringers (default 3)
      stringer_height  – stringer height [mm] (default = thickness * 5)
      stringer_width   – stringer foot width [mm] (default = thickness * 3)
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.cad.templates.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class StringerStiffenedPanelTemplate(BaseCADTemplate):
    """Flat skin panel with equally-spaced blade stringers."""

    geometry_type = "stringer_stiffened_panel"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        import cadquery as cq

        geo = spec.geometry
        L  = geo.length
        W  = geo.width
        Ts = geo.thickness   # skin thickness

        n  = int(geo.extra.get("n_stringers", 3))
        Sh = geo.extra.get("stringer_height", Ts * 5.0)
        Sw = geo.extra.get("stringer_width",  Ts * 2.0)

        logger.info(
            f"Building stringer_stiffened_panel: L={L} W={W} Ts={Ts} "
            f"n_stringers={n} Sh={Sh} Sw={Sw} mm"
        )

        # Skin plate
        panel = cq.Workplane("XY").box(L, W, Ts, centered=(True, True, False))

        # Add blade stringers (rectangular blade cross-section)
        if n > 0:
            pitch = W / (n + 1)
            for i in range(1, n + 1):
                y_pos = -W / 2 + pitch * i
                stringer = (
                    cq.Workplane("XY")
                    .box(L, Sw, Sh, centered=(True, True, False))
                    .translate((0.0, y_pos, Ts))
                )
                panel = panel.union(stringer)

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"
        cq.exporters.export(panel, str(step_path))
        logger.info(f"  STEP exported → {step_path}")

        total_height = Ts + Sh
        geometry_meta = GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces={},
            named_edges={},
            bounding_box={
                "xmin": -L / 2, "xmax": L / 2,
                "ymin": -W / 2, "ymax": W / 2,
                "zmin": 0.0,    "zmax": total_height,
            },
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "FIXED_END":   "Panel cross-section face at X = -L/2",
                "LOAD_END":    "Panel cross-section face at X = +L/2",
                "SKIN_BOTTOM": "Bottom face of skin at Z = 0",
            },
        )
