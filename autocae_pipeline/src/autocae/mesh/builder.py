"""Mesh Builder — 基于 Gmsh 的网格生成模块。

职责：
  1. 用 OpenCASCADE 内核导入 STEP 几何文件
  2. 用位置启发式方法（包围盒比较）自动分配 Physical Groups（命名集合）
  3. 根据 CaseSpec 的网格偏好设置全局/局部单元尺寸
  4. 生成并优化网格（Netgen 优化器）
  5. 导出 CalculiX 兼容的 .inp 网格文件（*NODE + *ELEMENT 块）
  6. 生成 mesh_groups.json 和 mesh_quality_report.json

输出文件：
    mesh.inp                 → 供 CalculiXAdapter 的 *INCLUDE 引用
    mesh_groups.json         → Physical Group 到求解器集合名的映射
    mesh_quality_report.json → 单元质量统计
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from loguru import logger

from autocae.cad.templates.base import CADResult
from autocae.schemas.case_spec import CaseSpec, ElementType
from autocae.schemas.mesh import (
    GeometryMeta,
    GeometrySource,
    MeshGroup,
    MeshGroups,
    MeshQualityReport,
    QualityCheck,
)


class MeshBuilder:
    """使用 Gmsh 从 STEP 文件生成 CalculiX 可用的网格。

    使用方式：
        builder = MeshBuilder()
        mesh_groups, quality = builder.build(spec, cad_result, output_dir)
    """

    def build(
        self,
        spec: CaseSpec,
        cad_result: CADResult,
        output_dir: Path,
    ) -> tuple[MeshGroups, MeshQualityReport]:
        """导入 STEP，划分网格，导出 CalculiX .inp。

        整个过程都在 try/finally 块中运行，确保 gmsh.finalize() 必然被调用
        （Gmsh 是单例，不 finalize 会导致后续调用出错）。

        Args:
            spec:       已验证的 CaseSpec（读取 mesh_preferences）
            cad_result: CADBuilder 的输出（含 STEP 路径和包围盒信息）
            output_dir: 运行目录

        Returns:
            (MeshGroups, MeshQualityReport) 元组
        """
        import gmsh

        prefs = spec.mesh_preferences
        step_path = str(cad_result.step_file)
        mesh_path = str(output_dir / "mesh.inp")
        geo_id = cad_result.geometry_meta.geometry_id

        logger.info(f"MeshBuilder: importing {step_path}")
        t0 = time.perf_counter()

        gmsh.initialize()
        gmsh.option.setNumber("General.Verbosity", 1)  # 减少 Gmsh 终端输出

        try:
            # ----------------------------------------------------------------
            # Step 1：导入 STEP 几何（使用 OpenCASCADE 内核）
            # ----------------------------------------------------------------
            gmsh.model.add("autocae_mesh")
            gmsh.model.occ.importShapes(step_path)  # 导入 STEP 文件
            gmsh.model.occ.synchronize()            # 同步 OCC 几何到 Gmsh 模型

            # ----------------------------------------------------------------
            # Step 2：用位置启发式方法分配 Physical Groups
            # ----------------------------------------------------------------
            # Physical Group 是 Gmsh 中的命名集合，导出到 .inp 后成为 CalculiX 的 *NSET/*ELSET
            groups = self._assign_physical_groups(spec, cad_result, geo_id)

            # ----------------------------------------------------------------
            # Step 3：设置全局/局部单元尺寸
            # ----------------------------------------------------------------
            # CharacteristicLengthMax = 单元最大尺寸（即 global_size）
            gmsh.option.setNumber("Mesh.CharacteristicLengthMax", prefs.global_size)
            gmsh.option.setNumber("Mesh.CharacteristicLengthMin", prefs.global_size * 0.1)

            # 局部加密：对指定命名区域设置更小的单元尺寸
            for region_name, local_size in prefs.local_refinements.items():
                try:
                    surf_tags = gmsh.model.getEntitiesForPhysicalGroup(2, _pg_tag(region_name, groups))
                    for tag in surf_tags:
                        gmsh.model.mesh.setSize(
                            gmsh.model.getBoundary([(2, tag)], oriented=False), local_size
                        )
                except Exception:
                    logger.warning(f"Could not apply local refinement for region '{region_name}'")

            # 根据 CaseSpec 的单元类型偏好设置单元阶次（1=线性，2=二次）
            self._set_element_order(prefs.element_type)

            # ----------------------------------------------------------------
            # Step 4：生成三维网格
            # ----------------------------------------------------------------
            logger.info(f"  Generating mesh (global size={prefs.global_size} mm)…")
            gmsh.model.mesh.generate(3)  # 3 表示生成三维体网格

            # 优化：使用 Netgen 优化器改善单元质量（重复 optimize_passes 次）
            for _ in range(prefs.optimize_passes):
                gmsh.model.mesh.optimize("Netgen")

            # ----------------------------------------------------------------
            # Step 5：导出 CalculiX .inp 格式的网格
            # ----------------------------------------------------------------
            output_dir.mkdir(parents=True, exist_ok=True)
            gmsh.write(mesh_path)  # Gmsh 根据 .inp 扩展名自动选择 Abaqus 格式
            logger.info(f"  Mesh exported → {mesh_path}")

            # ----------------------------------------------------------------
            # Step 6：计算网格质量统计
            # ----------------------------------------------------------------
            quality_report = self._compute_quality_report(geo_id, mesh_path, prefs.min_quality)

            mesh_groups = MeshGroups(
                geometry_id=geo_id,
                mesh_file=mesh_path,
                groups=groups,
                node_count=quality_report.node_count,
                element_count=quality_report.element_count,
            )

        finally:
            gmsh.finalize()  # 必须调用，释放 Gmsh 单例资源

        elapsed = time.perf_counter() - t0
        logger.info(f"  Mesh generation completed in {elapsed:.1f}s")

        # ----------------------------------------------------------------
        # Step 7：持久化 JSON 输出文件
        # ----------------------------------------------------------------
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
    # 私有辅助方法
    # ------------------------------------------------------------------

    def _assign_physical_groups(
        self,
        spec: CaseSpec,
        cad_result: CADResult,
        geo_id: str,
    ) -> list[MeshGroup]:
        """用包围盒位置启发式方法为各面分配 Gmsh Physical Groups。

        算法：
            1. 获取所有面（dim=2 的实体）
            2. 计算每个面的质心坐标（cx, cy, cz）
            3. 与包围盒的 xmin/xmax/zmax/zmin 对比（容差 = 总长度的 2%）：
                cx ≈ xmin → FIXED_END（固定端）
                cx ≈ xmax → LOAD_END（加载端）
                cz ≈ zmax → TOP_FACE（上表面）
                cz ≈ zmin → BOTTOM_FACE（下表面）
            4. 创建 Physical Group 并记录到 MeshGroup 列表
            5. 将所有体（dim=3）合并为 SOLID 体 Physical Group

        这种启发式方法对矩形平板效果最好。圆柱壳等其他几何可能需要子类覆盖。
        """
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
        tol  = (xmax - xmin) * 0.02   # 2% 的位置容差

        surfaces = gmsh.model.getEntities(2)  # 获取所有面

        # 初始化各命名面对应的 surface tag 列表
        named_surface_tags: dict[str, list[int]] = {
            "FIXED_END":   [],
            "LOAD_END":    [],
            "TOP_FACE":    [],
            "BOTTOM_FACE": [],
        }

        # 遍历所有面，用质心坐标归类到对应命名面
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

        # 为每个非空命名面创建 Physical Group
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
                        solver_set_name=name,  # 将在 job.inp 中作为 NSET/ELSET 名称
                        gmsh_entity_tags=tags,
                    )
                )
                pg_tag += 1

        # 将所有体合并为 SOLID Physical Group（用于 *SOLID SECTION 的 ELSET）
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
        """根据单元类型偏好设置网格阶次（1=线性，2=二次）。

        二次单元（C3D10, S8R, B32）精度更高但计算量更大，
        适合应力集中区域。一阶单元更适合屈曲和模态分析的初步计算。
        """
        import gmsh

        if element_type in (ElementType.C3D10, ElementType.S8R, ElementType.B32):
            gmsh.model.mesh.setOrder(2)  # 生成二次单元（每边中间节点）
        else:
            gmsh.model.mesh.setOrder(1)  # 生成一阶线性单元

    def _compute_quality_report(
        self, geo_id: str, mesh_file: str, min_quality_threshold: float
    ) -> MeshQualityReport:
        """计算网格质量统计指标。

        质量指标：Jacobian 行列式比（0~1，越接近 1 越好）。
            = 实际 Jacobian / 完美单元的 Jacobian
            < 0.3 通常认为质量不可接受，会导致求解精度下降甚至发散。

        算法：
            遍历所有单元，调用 Gmsh 的 getJacobians() 获取最小 Jacobian 值，
            统计全局最小值和平均值。
        """
        import gmsh
        import numpy as np

        # 获取所有单元类型和 tag
        element_types, element_tags, _ = gmsh.model.mesh.getElements()

        total_elements = sum(len(tags) for tags in element_tags)
        total_nodes = len(gmsh.model.mesh.getNodes()[0])

        # 计算每个单元的质量（使用 getElementQualities，返回 minSICN 值）
        # minSICN = 最小有符号归一化逆条件数（-1~1，越接近 1 越好，>0 为合格）
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
            min_q = avg_q = 1.0  # 若无法计算则假定完美质量

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
        warnings = [c.message for c in checks if not c.passed]

        return MeshQualityReport(
            geometry_id=geo_id,
            mesh_file=mesh_file,
            element_count=total_elements,
            node_count=total_nodes,
            min_quality=min_q,
            avg_quality=avg_q,
            max_aspect_ratio=0.0,   # Phase 2 扩展
            checks=checks,
            warnings=warnings,
            failed_checks=failed,
            overall_pass=len(failed) == 0,
        )


def _pg_tag(region_name: str, groups: list[MeshGroup]) -> int:
    """根据命名区域名称查找对应的 Gmsh Physical Group tag。

    Raises:
        KeyError: 若没有匹配的 Physical Group
    """
    for g in groups:
        if g.solver_set_name == region_name:
            return g.gmsh_tag
    raise KeyError(f"No physical group named '{region_name}'")
