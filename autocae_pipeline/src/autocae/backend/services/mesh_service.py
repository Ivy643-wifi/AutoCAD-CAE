"""Mesh service — 网格生成服务（Stage 3）。

职责：
  - 用 Gmsh 从 STEP 文件生成 CalculiX 可用的网格（mesh.inp）
  - 位置启发式分配 Physical Groups（命名集合）
  - 计算网格质量报告
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from loguru import logger

from autocae.backend.templates.cad.base import CADResult
from autocae.schemas.case_spec import CaseSpec, ElementType
from autocae.schemas.mesh import (
    GeometryMeta,
    GeometrySource,
    MeshGroup,
    MeshGroups,
    MeshQualityReport,
    QualityCheck,
)


class MeshService:
    """Stage 3 service: generate mesh from STEP file using Gmsh.

    Usage::

        service = MeshService()
        mesh_groups, quality = service.build(spec, cad_result, output_dir)
    """

    def build(
        self,
        spec: CaseSpec,
        cad_result: CADResult,
        output_dir: Path,
    ) -> tuple[MeshGroups, MeshQualityReport]:
        """Import STEP, generate mesh, export CalculiX .inp."""
        import gmsh

        prefs = spec.mesh_preferences
        step_path = str(cad_result.step_file)
        mesh_path = str(output_dir / "mesh.inp")
        geo_id = cad_result.geometry_meta.geometry_id

        logger.info(f"MeshService: importing {step_path}")
        t0 = time.perf_counter()

        gmsh.initialize()
        gmsh.option.setNumber("General.Verbosity", 1)

        try:
            gmsh.model.add("autocae_mesh")
            gmsh.model.occ.importShapes(step_path)
            gmsh.model.occ.synchronize()

            groups = self._assign_physical_groups(spec, cad_result, geo_id)

            gmsh.option.setNumber("Mesh.CharacteristicLengthMax", prefs.global_size)
            gmsh.option.setNumber("Mesh.CharacteristicLengthMin", prefs.global_size * 0.1)

            for region_name, local_size in prefs.local_refinements.items():
                try:
                    surf_tags = gmsh.model.getEntitiesForPhysicalGroup(
                        2, _pg_tag(region_name, groups)
                    )
                    for tag in surf_tags:
                        gmsh.model.mesh.setSize(
                            gmsh.model.getBoundary([(2, tag)], oriented=False), local_size
                        )
                except Exception:
                    logger.warning(f"Could not apply local refinement for region '{region_name}'")

            self._set_element_order(prefs.element_type)

            logger.info(f"  Generating mesh (global size={prefs.global_size} mm)…")
            gmsh.model.mesh.generate(3)

            for _ in range(prefs.optimize_passes):
                gmsh.model.mesh.optimize("Netgen")

            output_dir.mkdir(parents=True, exist_ok=True)
            gmsh.write(mesh_path)
            logger.info(f"  Mesh exported → {mesh_path}")

            quality_report = self._compute_quality_report(geo_id, mesh_path, prefs.min_quality)

            mesh_groups = MeshGroups(
                geometry_id=geo_id,
                mesh_file=mesh_path,
                groups=groups,
                node_count=quality_report.node_count,
                element_count=quality_report.element_count,
            )

        finally:
            gmsh.finalize()

        elapsed = time.perf_counter() - t0
        logger.info(f"  Mesh generation completed in {elapsed:.1f}s")

        groups_path = output_dir / "mesh_groups.json"
        groups_path.write_text(mesh_groups.to_json(), encoding="utf-8")
        logger.info(f"mesh_groups.json saved → {groups_path}")

        quality_path = output_dir / "mesh_quality_report.json"
        quality_path.write_text(quality_report.to_json(), encoding="utf-8")
        logger.info(f"mesh_quality_report.json saved → {quality_path}")

        if not quality_report.overall_pass:
            logger.warning(
                f"Mesh quality check FAILED. Minimum quality: "
                f"{quality_report.min_quality:.3f} (threshold: {prefs.min_quality})"
            )

        return mesh_groups, quality_report

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _assign_physical_groups(
        self,
        spec: CaseSpec,
        cad_result: CADResult,
        geo_id: str,
    ) -> list[MeshGroup]:
        """Assign Gmsh Physical Groups using bounding-box position heuristics."""
        import gmsh

        groups: list[MeshGroup] = []
        bbox = cad_result.geometry_meta.bounding_box

        if not bbox:
            logger.warning("No bounding box in GeometryMeta; skipping named group assignment.")
            return groups

        xmin = bbox.get("xmin", 0.0)
        xmax = bbox.get("xmax", 0.0)
        zmax = bbox.get("zmax", 0.0)
        zmin = bbox.get("zmin", 0.0)
        tol  = (xmax - xmin) * 0.02

        surfaces = gmsh.model.getEntities(2)
        named_surface_tags: dict[str, list[int]] = {
            "FIXED_END":   [],
            "LOAD_END":    [],
            "TOP_FACE":    [],
            "BOTTOM_FACE": [],
        }

        for dim, tag in surfaces:
            cx, cy, cz = gmsh.model.occ.getCenterOfMass(dim, tag)
            if abs(cx - xmin) < tol:
                named_surface_tags["FIXED_END"].append(tag)
            elif abs(cx - xmax) < tol:
                named_surface_tags["LOAD_END"].append(tag)
            elif abs(cz - zmax) < tol:
                named_surface_tags["TOP_FACE"].append(tag)
            elif abs(cz - zmin) < tol:
                named_surface_tags["BOTTOM_FACE"].append(tag)

        pg_tag = 1
        for name, tags in named_surface_tags.items():
            if tags:
                gmsh.model.addPhysicalGroup(2, tags, pg_tag)
                gmsh.model.setPhysicalName(2, pg_tag, name)
                groups.append(
                    MeshGroup(
                        group_id=f"pg_{name.lower()}",
                        entity_type="surface",
                        gmsh_tag=pg_tag,
                        mapped_region=name,
                        solver_set_name=name,
                        gmsh_entity_tags=tags,
                    )
                )
                pg_tag += 1

        volumes = gmsh.model.getEntities(3)
        if volumes:
            vol_tags = [t for _, t in volumes]
            gmsh.model.addPhysicalGroup(3, vol_tags, pg_tag)
            gmsh.model.setPhysicalName(3, pg_tag, "SOLID")
            groups.append(
                MeshGroup(
                    group_id="pg_solid",
                    entity_type="volume",
                    gmsh_tag=pg_tag,
                    mapped_region="SOLID",
                    solver_set_name="SOLID",
                    gmsh_entity_tags=vol_tags,
                )
            )

        logger.info(f"  {len(groups)} physical groups assigned.")
        return groups

    def _set_element_order(self, element_type: ElementType) -> None:
        import gmsh
        if element_type in (ElementType.C3D10, ElementType.S8R, ElementType.B32):
            gmsh.model.mesh.setOrder(2)
        else:
            gmsh.model.mesh.setOrder(1)

    def _compute_quality_report(
        self, geo_id: str, mesh_file: str, min_quality_threshold: float
    ) -> MeshQualityReport:
        import gmsh
        import numpy as np

        element_types, element_tags, _ = gmsh.model.mesh.getElements()
        total_elements = sum(len(tags) for tags in element_tags)
        total_nodes = len(gmsh.model.mesh.getNodes()[0])

        qualities: list[float] = []
        for etype, etags in zip(element_types, element_tags):
            if len(etags) == 0:
                continue
            try:
                q_vals = gmsh.model.mesh.getElementQualities(etags, "minSICN")
                qualities.extend(float(v) for v in q_vals)
            except Exception:
                pass

        if qualities:
            min_q = float(min(qualities))
            avg_q = float(sum(qualities) / len(qualities))
        else:
            min_q = avg_q = 1.0

        checks: list[QualityCheck] = [
            QualityCheck(
                check_name="min_jacobian_quality",
                passed=min_q >= min_quality_threshold,
                value=min_q,
                threshold=min_quality_threshold,
                message="" if min_q >= min_quality_threshold else
                         f"Min quality {min_q:.3f} < threshold {min_quality_threshold}",
            )
        ]

        failed = [c.check_name for c in checks if not c.passed]

        return MeshQualityReport(
            geometry_id=geo_id,
            mesh_file=mesh_file,
            element_count=total_elements,
            node_count=total_nodes,
            min_quality=min_q,
            avg_quality=avg_q,
            max_aspect_ratio=0.0,
            checks=checks,
            warnings=[c.message for c in checks if not c.passed],
            failed_checks=failed,
            overall_pass=len(failed) == 0,
        )


def _pg_tag(region_name: str, groups: list[MeshGroup]) -> int:
    for g in groups:
        if g.solver_set_name == region_name:
            return g.gmsh_tag
    raise KeyError(f"No physical group named '{region_name}'")
