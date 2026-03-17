"""Run retention and archive service (M2.5).

提供 run 长期保留规则和归档命令，不破坏审计链。

存储结构：
    runs/
    ├── <case_id>/          ← 活跃 run
    │   ├── case_spec.json
    │   └── ...
    └── .archive/           ← 归档目录
        ├── <case_id>/      ← 归档后的 run（结构不变）
        │   └── ...
        └── archive_manifest.jsonl  ← append-only 归档记录
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field


class RetentionPolicy(BaseModel):
    """Run 保留策略配置（M2.5）。"""
    max_age_days: int = Field(
        default=90,
        ge=1,
        description="成功 run 的最大保留天数（超过后归档）",
    )
    max_failed_age_days: int = Field(
        default=30,
        ge=1,
        description="失败 run 的最大保留天数（通常比成功 run 短）",
    )
    archive_subdir: str = Field(
        default=".archive",
        description="归档子目录名（相对于 runs_dir）",
    )


class RetentionService:
    """Run 归档与保留服务（M2.5）。

    完成标准：
        1. 不破坏审计链（归档时复制全部产物，保留 index.jsonl 引用）
        2. 可控存储增长（按策略批量归档旧 run）
        3. 支持恢复（restore 命令）
    """

    def __init__(self, runs_dir: Path | str = Path("runs")) -> None:
        self.runs_dir = Path(runs_dir)
        self.archive_dir = self.runs_dir / ".archive"
        self.manifest_path = self.archive_dir / "archive_manifest.jsonl"

    # ------------------------------------------------------------------
    # Archive single run
    # ------------------------------------------------------------------

    def archive_run(self, case_id: str) -> Path:
        """归档单个 run（M2.5）。

        将 runs/<case_id>/ 整体移动到 runs/.archive/<case_id>/。
        在归档记录中写入原始路径，确保审计链不中断。

        Returns:
            归档后的目录路径
        """
        run_dir = self.runs_dir / case_id
        if not run_dir.exists():
            raise FileNotFoundError(f"Run directory not found: {run_dir}")

        archived_dir = self.archive_dir / case_id
        if archived_dir.exists():
            raise FileExistsError(
                f"Already archived: {archived_dir}. "
                "Remove the existing archive first if you want to re-archive."
            )

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(run_dir), str(archived_dir))

        meta = self._read_run_meta(archived_dir)
        self._append_manifest(
            action="archive",
            case_id=case_id,
            run_dir=str(run_dir),
            archived_dir=str(archived_dir),
            meta=meta,
        )
        # 在 runs/index.jsonl 补写一条 archive 记录（不破坏审计链）
        self._append_run_index(
            case_id=case_id,
            run_dir=str(archived_dir),
            action="archived",
            meta=meta,
        )

        logger.info(f"[RetentionService] Archived {case_id} → {archived_dir}")
        return archived_dir

    # ------------------------------------------------------------------
    # Restore archived run
    # ------------------------------------------------------------------

    def restore_run(self, case_id: str) -> Path:
        """从归档中恢复 run（M2.5）。

        Returns:
            恢复后的 runs/<case_id>/ 路径
        """
        archived_dir = self.archive_dir / case_id
        if not archived_dir.exists():
            raise FileNotFoundError(f"Archived run not found: {archived_dir}")

        run_dir = self.runs_dir / case_id
        if run_dir.exists():
            raise FileExistsError(
                f"Run directory already exists: {run_dir}. "
                "Remove it first before restoring."
            )

        shutil.move(str(archived_dir), str(run_dir))
        meta = self._read_run_meta(run_dir)
        self._append_manifest(
            action="restore",
            case_id=case_id,
            run_dir=str(run_dir),
            archived_dir=str(archived_dir),
            meta=meta,
        )
        self._append_run_index(
            case_id=case_id,
            run_dir=str(run_dir),
            action="restored",
            meta=meta,
        )

        logger.info(f"[RetentionService] Restored {case_id} → {run_dir}")
        return run_dir

    # ------------------------------------------------------------------
    # Apply policy (batch archive)
    # ------------------------------------------------------------------

    def apply_policy(
        self,
        policy: RetentionPolicy | None = None,
    ) -> list[str]:
        """按策略批量归档超期 run（M2.5）。

        Returns:
            被归档的 case_id 列表
        """
        if policy is None:
            policy = RetentionPolicy()

        now = datetime.now(timezone.utc)
        archived_ids: list[str] = []

        if not self.runs_dir.exists():
            return archived_ids

        for run_dir in sorted(self.runs_dir.iterdir()):
            if not run_dir.is_dir() or run_dir.name.startswith("."):
                continue

            meta = self._read_run_meta(run_dir)
            created_at_str = meta.get("created_at_utc") or meta.get("updated_at_utc")
            if not created_at_str:
                # 用目录 mtime 作为近似值
                import os
                mtime = os.path.getmtime(run_dir)
                created_at = datetime.fromtimestamp(mtime, tz=timezone.utc)
            else:
                try:
                    created_at = datetime.fromisoformat(created_at_str)
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue

            age_days = (now - created_at).days
            is_success = meta.get("success", True)
            max_days = policy.max_age_days if is_success else policy.max_failed_age_days

            if age_days >= max_days:
                try:
                    self.archive_run(run_dir.name)
                    archived_ids.append(run_dir.name)
                    logger.info(
                        f"[RetentionService] Policy: archived {run_dir.name} "
                        f"(age={age_days}d >= max={max_days}d)"
                    )
                except Exception as exc:
                    logger.warning(
                        f"[RetentionService] Failed to archive {run_dir.name}: {exc}"
                    )

        return archived_ids

    # ------------------------------------------------------------------
    # List archived runs
    # ------------------------------------------------------------------

    def list_archived(self) -> list[dict[str, Any]]:
        """列出所有已归档的 run。"""
        if not self.archive_dir.exists():
            return []
        result = []
        for d in sorted(self.archive_dir.iterdir()):
            if d.is_dir():
                meta = self._read_run_meta(d)
                result.append({"case_id": d.name, "archived_dir": str(d), **meta})
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_run_meta(run_dir: Path) -> dict[str, Any]:
        """读取 issue_report.json 中的运行元数据（尽力而为）。"""
        issue_path = run_dir / "issue_report.json"
        if issue_path.exists():
            try:
                return json.loads(issue_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _append_manifest(
        self,
        *,
        action: str,
        case_id: str,
        run_dir: str,
        archived_dir: str,
        meta: dict[str, Any],
    ) -> None:
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "case_id": case_id,
            "run_dir": run_dir,
            "archived_dir": archived_dir,
            "success": meta.get("success"),
            "wall_time_s": meta.get("wall_time_s"),
        }
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _append_run_index(
        self,
        *,
        case_id: str,
        run_dir: str,
        action: str,
        meta: dict[str, Any],
    ) -> None:
        index_path = self.runs_dir / "index.jsonl"
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "entry_type": action,
            "case_id": case_id,
            "run_dir": run_dir,
            "success": meta.get("success"),
            "wall_time_s": meta.get("wall_time_s"),
            "error_stage": meta.get("error_stage", "none"),
        }
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
