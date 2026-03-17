"""Project Case Library service (M2.1).

提供 run 成功后落库、检索、列表等操作。
存储结构：
    project_case_library/
    └── cases/
        ├── <case_id>.json
        └── ...
    └── index.jsonl   (append-only 时间线索引，含检索字段)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from autocae.schemas.project_case import (
    ProjectCase,
    ProjectCaseComputationView,
    ProjectCaseEngineeringView,
)


class ProjectCaseLibrary:
    """项目案例库（M2.1）。

    职责：
        - 从成功的 PipelineResult + CaseSpec 构建 ProjectCase 记录
        - 持久化到 project_case_library/cases/<case_id>.json
        - 维护 index.jsonl（append-only，含检索字段）
        - 提供简单关键字搜索（不依赖外部向量库）
        - 从 ProjectCase 反查对应 run 与产物（reverse lookup）
    """

    def __init__(self, library_dir: Path | str = Path("project_case_library")) -> None:
        self.library_dir = Path(library_dir)
        self.cases_dir = self.library_dir / "cases"
        self.index_path = self.library_dir / "index.jsonl"

    def _ensure_dirs(self) -> None:
        self.cases_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Save (run → ProjectCase)
    # ------------------------------------------------------------------

    def save_from_run(
        self,
        *,
        result: Any,          # PipelineResult (避免循环 import，用 Any)
        spec: Any,            # CaseSpec
    ) -> Path:
        """将成功的 run 落库为 ProjectCase 记录（M2.1 完成标准）。

        Args:
            result: PipelineResult 实例
            spec:   CaseSpec 实例

        Returns:
            保存的 .json 文件路径
        """
        self._ensure_dirs()

        eng_view = self._build_engineering_view(spec)
        comp_view = self._build_computation_view(result, spec)

        pc = ProjectCase(
            source_case_id=spec.metadata.case_id,
            case_name=spec.metadata.case_name,
            run_dir=str(result.run_dir),
            source_spec_path=str(result.run_dir / "case_spec.json"),
            template_id=spec.metadata.template_id,
            template_affinity=spec.metadata.template_affinity,
            template_link=spec.metadata.template_link,
            status="completed" if result.success else "failed",
            engineering_view=eng_view,
            computation_view=comp_view,
        )

        case_path = self.cases_dir / f"{pc.source_case_id}.json"
        case_path.write_text(pc.to_json(), encoding="utf-8")
        self._append_index(pc)

        logger.info(f"[ProjectCaseLibrary] Saved case {pc.source_case_id} → {case_path}")
        return case_path

    @staticmethod
    def _build_engineering_view(spec: Any) -> ProjectCaseEngineeringView:
        geo = spec.geometry
        geo_summary = (
            f"length={geo.length}mm, width={geo.width}mm, thickness={geo.thickness}mm"
        )
        extra_parts = [f"{k}={v}" for k, v in geo.extra.items()]
        if extra_parts:
            geo_summary += ", " + ", ".join(extra_parts)

        mat_parts = []
        for m in spec.materials:
            if m.E is not None:
                mat_parts.append(f"{m.name}(E={m.E}MPa, nu={m.nu})")
            elif m.E1 is not None:
                mat_parts.append(f"{m.name}(E1={m.E1}MPa, E2={m.E2}MPa)")
        mat_summary = "; ".join(mat_parts) if mat_parts else "N/A"

        load_parts = [f"{ld.load_type.value} {ld.magnitude} at {ld.location}" for ld in spec.loads]
        bc_parts = [f"{bc.bc_type.value} at {bc.location}" for bc in spec.boundary_conditions]
        features = [f.name.value for f in spec.features if f.enabled]

        return ProjectCaseEngineeringView(
            geometry_type=geo.geometry_type.value,
            topology=spec.topology.value,
            analysis_type=spec.analysis_type.value,
            geometry_summary=geo_summary,
            material_summary=mat_summary,
            load_summary="; ".join(load_parts),
            bc_summary="; ".join(bc_parts),
            features=features,
        )

    @staticmethod
    def _build_computation_view(result: Any, spec: Any) -> ProjectCaseComputationView:
        mp = spec.mesh_preferences
        mq = result.mesh_quality
        rs = result.result_summary

        node_count = element_count = None
        min_quality = overall_pass = None
        if mq is not None:
            m = mq.metrics
            node_count = getattr(m, "node_count", None)
            element_count = getattr(m, "element_count", None)
            min_quality = getattr(m, "min_jacobian", None)
            overall_pass = mq.overall_pass

        max_disp = max_mises = blf = None
        nat_freqs: list[float] = []
        if rs is not None:
            max_disp = rs.max_displacement
            max_mises = rs.max_mises_stress
            blf = rs.buckling_load_factor
            nat_freqs = list(rs.natural_frequencies or [])

        return ProjectCaseComputationView(
            mesh_global_size=mp.global_size,
            mesh_element_type=mp.element_type.value,
            mesh_node_count=node_count,
            mesh_element_count=element_count,
            mesh_min_quality=min_quality,
            mesh_overall_pass=overall_pass,
            dry_run=getattr(result, "dry_run", False),
            wall_time_s=result.wall_time_s,
            max_displacement=max_disp,
            max_mises_stress=max_mises,
            buckling_load_factor=blf,
            natural_frequencies=nat_freqs,
        )

    def _append_index(self, pc: ProjectCase) -> None:
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "case_id": pc.case_id,
            "source_case_id": pc.source_case_id,
            "case_name": pc.case_name,
            "geometry_type": pc.engineering_view.geometry_type,
            "topology": pc.engineering_view.topology,
            "analysis_type": pc.engineering_view.analysis_type,
            "status": pc.status,
            "template_id": pc.template_id,
            "template_affinity": pc.template_affinity,
            "run_dir": pc.run_dir,
            "case_path": str(self.cases_dir / f"{pc.source_case_id}.json"),
        }
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        with self.index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Load & list
    # ------------------------------------------------------------------

    def load(self, source_case_id: str) -> ProjectCase:
        """根据 source_case_id 加载 ProjectCase 记录（M2.1 反查）。"""
        path = self.cases_dir / f"{source_case_id}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"ProjectCase '{source_case_id}' not found in {self.cases_dir}."
            )
        return ProjectCase.from_json(str(path))

    def list_all(self) -> list[ProjectCase]:
        """列出所有已落库的 ProjectCase 记录。"""
        if not self.cases_dir.exists():
            return []
        results = []
        for p in sorted(self.cases_dir.glob("*.json")):
            try:
                results.append(ProjectCase.from_json(str(p)))
            except Exception as exc:
                logger.warning(f"[ProjectCaseLibrary] Failed to load {p}: {exc}")
        return results

    def search(
        self,
        *,
        query: str = "",
        geometry_type: str | None = None,
        analysis_type: str | None = None,
        max_results: int = 20,
    ) -> list[ProjectCase]:
        """简单关键字 + 字段过滤搜索（M2.1 检索）。

        按 template_affinity 降序排序（M2.3），affinity 为 None 时排最后。
        """
        all_cases = self.list_all()
        query_lower = query.lower()

        def _match(pc: ProjectCase) -> bool:
            ev = pc.engineering_view
            if geometry_type and ev.geometry_type != geometry_type:
                return False
            if analysis_type and ev.analysis_type != analysis_type:
                return False
            if query_lower:
                searchable = " ".join([
                    pc.case_name,
                    ev.geometry_type,
                    ev.topology,
                    ev.analysis_type,
                    ev.geometry_summary,
                    ev.material_summary,
                    pc.template_id or "",
                ]).lower()
                return query_lower in searchable
            return True

        matched = [pc for pc in all_cases if _match(pc)]
        # M2.3: 按 template_affinity 降序排序
        matched.sort(
            key=lambda pc: pc.template_affinity if pc.template_affinity is not None else -1.0,
            reverse=True,
        )
        return matched[:max_results]

    def reverse_lookup(self, source_case_id: str) -> dict[str, Any]:
        """从 ProjectCase 反查对应 run 产物路径（M2.1 完成标准）。"""
        pc = self.load(source_case_id)
        run_dir = Path(pc.run_dir)
        artifacts: dict[str, str] = {}
        for name in [
            "case_spec.json", "model.step", "geometry_meta.json",
            "mesh.inp", "mesh_groups.json", "mesh_quality_report.json",
            "analysis_model.json", "solver_job.json", "job.inp", "job.frd",
            "run_status.json", "review_transcript.json", "issue_report.json",
            "field_manifest.json", "diagnostics.json",
        ]:
            p = run_dir / name
            if p.exists():
                artifacts[name] = str(p)
        return {
            "source_case_id": pc.source_case_id,
            "case_name": pc.case_name,
            "run_dir": str(run_dir),
            "status": pc.status,
            "artifacts": artifacts,
        }
