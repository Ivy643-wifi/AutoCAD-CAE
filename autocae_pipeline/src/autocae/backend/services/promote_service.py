"""Template Candidate + Promote service (M2.2).

实现流程：run review -> approved -> template candidate -> promote

存储结构：
    template_library/
    └── candidates/
        ├── <candidate_id>.json
        └── ...
    └── candidates_index.jsonl  (append-only)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Template Candidate schema
# ---------------------------------------------------------------------------

class TemplateCandidate(BaseModel):
    """模板候选记录（M2.2）。

    表示一个等待或已完成 promote 审批的 run 产物。
    完整 provenance 记录可回溯至原始 run。
    """
    candidate_id: str = Field(default_factory=lambda: f"cand_{uuid.uuid4().hex[:8]}")
    source_case_id: str                            # 原始 CaseSpec case_id
    source_run_dir: str                            # 对应 runs/<case_id>/ 路径
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    submitted_by: str = "user"
    status: str = "pending"                        # "pending" | "approved" | "rejected"
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    review_notes: str = ""
    promoted_template_id: str | None = None        # 审批后生成的 template ID
    provenance: dict[str, Any] = Field(default_factory=dict)  # 原始 run 元数据

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, path: str) -> "TemplateCandidate":
        from pathlib import Path as _Path
        return cls.model_validate_json(_Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Promote service
# ---------------------------------------------------------------------------

class PromoteService:
    """模板提升服务（M2.2）。

    职责：
        - 从成功的 run 提交 TemplateCandidate
        - 审批 (approve) / 拒绝 (reject) 候选
        - 审批通过后注册为 template_library/candidates/<id>.json
        - 所有操作写入 candidates_index.jsonl（append-only，可回溯）
    """

    def __init__(
        self,
        candidates_dir: Path | str = Path("template_library/candidates"),
    ) -> None:
        self.candidates_dir = Path(candidates_dir)
        self.index_path = self.candidates_dir.parent / "candidates_index.jsonl"

    def _ensure_dirs(self) -> None:
        self.candidates_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    def submit_candidate(
        self,
        *,
        source_case_id: str,
        run_dir: Path | str,
        submitted_by: str = "user",
    ) -> TemplateCandidate:
        """从成功的 run 提交候选模板（M2.2 主流程入口）。

        Args:
            source_case_id: CaseSpec 的 case_id
            run_dir: runs/<case_id>/ 目录路径
            submitted_by: 提交人（默认 "user"）

        Returns:
            创建的 TemplateCandidate 记录
        """
        self._ensure_dirs()
        run_dir = Path(run_dir)

        # 构建 provenance（追溯信息）
        provenance = self._build_provenance(source_case_id, run_dir)

        cand = TemplateCandidate(
            source_case_id=source_case_id,
            source_run_dir=str(run_dir),
            submitted_by=submitted_by,
            status="pending",
            provenance=provenance,
        )

        cand_path = self.candidates_dir / f"{cand.candidate_id}.json"
        cand_path.write_text(cand.to_json(), encoding="utf-8")
        self._append_index(cand, action="submit")
        logger.info(
            f"[PromoteService] Candidate {cand.candidate_id} submitted "
            f"from run {source_case_id}"
        )
        return cand

    @staticmethod
    def _build_provenance(source_case_id: str, run_dir: Path) -> dict[str, Any]:
        provenance: dict[str, Any] = {
            "source_case_id": source_case_id,
            "run_dir": str(run_dir),
            "submitted_at_utc": datetime.now(timezone.utc).isoformat(),
            "artifacts": {},
        }
        for name in [
            "case_spec.json", "model.step", "mesh.inp",
            "analysis_model.json", "solver_job.json",
            "run_status.json", "issue_report.json",
        ]:
            p = run_dir / name
            if p.exists():
                provenance["artifacts"][name] = str(p)
        # 读取 case_spec.json 摘要信息
        spec_path = run_dir / "case_spec.json"
        if spec_path.exists():
            try:
                spec_data = json.loads(spec_path.read_text(encoding="utf-8"))
                meta = spec_data.get("metadata", {})
                geo = spec_data.get("geometry", {})
                provenance["case_name"] = meta.get("case_name", "")
                provenance["geometry_type"] = geo.get("geometry_type", "")
                provenance["analysis_type"] = spec_data.get("analysis_type", "")
                provenance["template_id"] = meta.get("template_id")
            except Exception:
                pass
        return provenance

    # ------------------------------------------------------------------
    # Approve / Reject
    # ------------------------------------------------------------------

    def approve_candidate(
        self,
        candidate_id: str,
        *,
        reviewed_by: str = "user",
        review_notes: str = "",
    ) -> TemplateCandidate:
        """审批通过候选模板，生成 promoted_template_id（M2.2）。"""
        cand = self._load_candidate(candidate_id)
        if cand.status != "pending":
            raise ValueError(
                f"Candidate {candidate_id} is not in 'pending' state (current: {cand.status})."
            )
        promoted_id = (
            f"tmpl_{cand.provenance.get('geometry_type', 'unknown')}"
            f"_{cand.candidate_id}"
        )
        cand.status = "approved"
        cand.reviewed_at = datetime.now(timezone.utc)
        cand.reviewed_by = reviewed_by
        cand.review_notes = review_notes
        cand.promoted_template_id = promoted_id
        self._save_candidate(cand)
        self._append_index(cand, action="approve")
        logger.info(
            f"[PromoteService] Candidate {candidate_id} approved → template {promoted_id}"
        )
        return cand

    def reject_candidate(
        self,
        candidate_id: str,
        *,
        reason: str,
        reviewed_by: str = "user",
    ) -> TemplateCandidate:
        """拒绝候选模板（M2.2）。"""
        cand = self._load_candidate(candidate_id)
        if cand.status != "pending":
            raise ValueError(
                f"Candidate {candidate_id} is not in 'pending' state (current: {cand.status})."
            )
        cand.status = "rejected"
        cand.reviewed_at = datetime.now(timezone.utc)
        cand.reviewed_by = reviewed_by
        cand.review_notes = reason
        self._save_candidate(cand)
        self._append_index(cand, action="reject")
        logger.info(f"[PromoteService] Candidate {candidate_id} rejected: {reason}")
        return cand

    # ------------------------------------------------------------------
    # List / Load
    # ------------------------------------------------------------------

    def list_candidates(
        self,
        status_filter: str | None = None,
    ) -> list[TemplateCandidate]:
        """列出所有候选，可按状态过滤（M2.2）。"""
        if not self.candidates_dir.exists():
            return []
        results = []
        for p in sorted(self.candidates_dir.glob("*.json")):
            try:
                cand = TemplateCandidate.from_json(str(p))
                if status_filter is None or cand.status == status_filter:
                    results.append(cand)
            except Exception as exc:
                logger.warning(f"[PromoteService] Failed to load {p}: {exc}")
        return results

    def _load_candidate(self, candidate_id: str) -> TemplateCandidate:
        path = self.candidates_dir / f"{candidate_id}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"TemplateCandidate '{candidate_id}' not found in {self.candidates_dir}."
            )
        return TemplateCandidate.from_json(str(path))

    def _save_candidate(self, cand: TemplateCandidate) -> None:
        path = self.candidates_dir / f"{cand.candidate_id}.json"
        path.write_text(cand.to_json(), encoding="utf-8")

    def _append_index(self, cand: TemplateCandidate, action: str) -> None:
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "candidate_id": cand.candidate_id,
            "source_case_id": cand.source_case_id,
            "status": cand.status,
            "submitted_by": cand.submitted_by,
            "reviewed_by": cand.reviewed_by,
            "promoted_template_id": cand.promoted_template_id,
        }
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        with self.index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
