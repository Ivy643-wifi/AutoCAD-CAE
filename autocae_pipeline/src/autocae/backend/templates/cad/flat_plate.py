"""平板（Flat Plate）CAD 模板。"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from autocae.backend.templates.cad.base import BaseCADTemplate, CADResult
from autocae.schemas.case_spec import CaseSpec, FeatureName
from autocae.schemas.mesh import GeometryMeta, GeometrySource


class FlatPlateTemplate(BaseCADTemplate):
    """矩形平板模板（可选开孔特征）。"""

    geometry_type = "flat_plate"

    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        import cadquery as cq

        geo = spec.geometry
        L = geo.length
        W = geo.width
        T = geo.thickness

        logger.info(f"Building flat_plate: L={L} W={W} T={T} mm")

        plate = (
            cq.Workplane("XY")
            .box(L, W, T, centered=(True, True, True))
        )

        hole_feature = next(
            (f for f in spec.features if f.name == FeatureName.HOLE and f.enabled), None
        )
        if hole_feature:
            d = hole_feature.params.get("diameter", W * 0.2)
            logger.info(f"  Adding hole: diameter={d} mm (centre of plate)")
            plate = plate.faces(">Z").workplane().hole(d)

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
            volume=L * W * T,
        )

        return CADResult(
            step_file=step_path,
            geometry_meta=geometry_meta,
            named_faces={
                "FIXED_END": "Face at X = -{L/2} (clamped boundary)",
                "LOAD_END":  "Face at X = +{L/2} (applied load)",
                "TOP_FACE":  "Face at Z = +{T/2}",
                "BOTTOM_FACE": "Face at Z = -{T/2}",
            },
        )
