"""Bolted lap joint CAD template.

Supported analysis types: tension, shear.

Geometry:
  - Two overlapping plate segments joined by fastener holes.
  - geometry.extra keys:
      overlap_length   – overlap zone length [mm] (default = length * 0.2)
      n_bolts          – number of bolt holes in a row (default 2)
      bolt_diameter    – bolt hole diameter [mm] (default = thickness * 2)
      bolt_pitch       – bolt centre-to-centre spacing [mm] (default = width / (n_bolts + 1))
"""

from __future__ import annotations

import math
from pathlib import Path

from loguru import logger

from autocae.cad.templates.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class BoltedLapJointTemplate(BaseCADTemplate):
    """Two-plate bolted lap joint (single shear configuration)."""

    geometry_type = "bolted_lap_joint"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        import cadquery as cq

        geo = spec.geometry
        L   = geo.length
        W   = geo.width
        T   = geo.thickness
        Lo  = geo.extra.get("overlap_length", L * 0.2)
        n   = int(geo.extra.get("n_bolts", 2))
        Db  = geo.extra.get("bolt_diameter", T * 2.0)

        # Bolt pitch along Y (across width)
        pitch = geo.extra.get("bolt_pitch", W / (n + 1))

        logger.info(
            f"Building bolted_lap_joint: L={L} W={W} T={T} "
            f"overlap={Lo} n_bolts={n} bolt_d={Db} mm"
        )

        # --- Plate A: extends from X = -L/2 to X = Lo/2 ---
        plate_a = cq.Workplane("XY").box(
            L / 2 + Lo / 2, W, T, centered=(False, True, True)
        ).translate((-L / 2, 0, 0))

        # --- Plate B: extends from X = -Lo/2 to X = L/2, offset in Z ---
        plate_b = cq.Workplane("XY").box(
            L / 2 + Lo / 2, W, T, centered=(False, True, True)
        ).translate((-Lo / 2, 0, T))          # offset by one thickness

        # Punch bolt holes in both plates
        for i in range(1, n + 1):
            y_pos = -W / 2 + pitch * i
            # Hole in plate A (within overlap zone)
            plate_a = (
                plate_a
                .faces(">Z")
                .workplane()
                .center(0, y_pos)
                .hole(Db, T)                   # through-hole
            )
            # Hole in plate B
            plate_b = (
                plate_b
                .faces(">Z")
                .workplane()
                .center(0, y_pos)
                .hole(Db, T)
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        step_path = output_dir / "model.step"

        # Export as assembly (compound of two solids)
        compound = plate_a.val().fuse(plate_b.val())

        import cadquery as cq
        compound_wp = cq.Workplane().newObject([compound])
        cq.exporters.export(compound_wp, str(step_path))
        logger.info(f"  STEP exported → {step_path}")

        geometry_meta = GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces={},
            named_edges={},
            bounding_box={
                "xmin": -L / 2, "xmax": L / 2,
                "ymin": -W / 2, "ymax": W / 2,
                "zmin": -T / 2, "zmax": T * 3 / 2,
            },
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "PLATE_A_GRIP": "Free end of plate A (X = -L/2)",
                "PLATE_B_GRIP": "Free end of plate B (X = +L/2)",
                "BOLT_HOLES":   "Cylindrical bore surfaces of bolt holes",
            },
        )
