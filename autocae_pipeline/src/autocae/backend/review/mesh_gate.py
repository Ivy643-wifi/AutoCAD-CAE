"""Mesh Gate service — auto_check + preview + user_confirm + transcript."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from autocae.backend.input.validator import DiagnosticsValidator
from autocae.backend.services.visualization_service import VisualizationService

GateDecision = Literal["confirm", "edit", "abort"]


@dataclass
class MeshGateOutcome:
    """Mesh Gate 执行结果。"""

    run_dir: Path
    mesh_file: Path
    mesh_groups_file: Path | None
    mesh_quality_file: Path | None
    preview_png: Path | None
    decision: GateDecision
    auto_check_passed: bool
    next_stage_allowed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    transcript_path: Path | None = None


class MeshGateService:
    """执行 Mesh 阶段 gate：auto_check -> preview -> user_confirm -> transcript."""

    def __init__(
        self,
        visualization_service: VisualizationService | None = None,
        diagnostics_validator: DiagnosticsValidator | None = None,
    ) -> None:
        self._viz = visualization_service or VisualizationService()
        self._diag = diagnostics_validator or DiagnosticsValidator()

    def run_gate(
        self,
        *,
        run_dir: Path,
        decision: GateDecision,
        comment: str = "",
        edit_request: str = "",
        interactive_preview: bool = False,
    ) -> MeshGateOutcome:
        run_dir = Path(run_dir).resolve()
        mesh_file = self._resolve_artifact(run_dir, ["mesh.inp", "03_mesh/mesh.inp"])
        mesh_groups_file = self._resolve_artifact(
            run_dir,
            ["mesh_groups.json", "03_mesh/mesh_groups.json"],
            required=False,
        )
        mesh_quality_file = self._resolve_artifact(
            run_dir,
            ["mesh_quality_report.json", "03_mesh/mesh_quality_report.json"],
            required=False,
        )

        checks = self._run_auto_checks(
            mesh_file=mesh_file,
            mesh_groups_file=mesh_groups_file,
            mesh_quality_file=mesh_quality_file,
        )
        auto_check_passed = all(c["passed"] for c in checks)

        preview_png, preview_check = self._generate_preview(
            mesh_file=mesh_file,
            mesh_groups_file=mesh_groups_file,
            interactive_preview=interactive_preview,
        )
        checks.append(preview_check)
        auto_check_passed = auto_check_passed and preview_check["passed"]

        if decision == "confirm" and not auto_check_passed:
            raise ValueError(
                "Mesh auto-check did not pass. Use decision 'edit' or 'abort' before solver stage."
            )

        next_stage_allowed = decision == "confirm" and auto_check_passed
        transcript_path = self._append_transcript(
            run_dir=run_dir,
            mesh_file=mesh_file,
            mesh_groups_file=mesh_groups_file,
            mesh_quality_file=mesh_quality_file,
            preview_png=preview_png,
            decision=decision,
            comment=comment,
            edit_request=edit_request,
            checks=checks,
            next_stage_allowed=next_stage_allowed,
        )

        logger.info(
            f"Mesh Gate finished: decision={decision}, "
            f"auto_check_passed={auto_check_passed}, next_stage_allowed={next_stage_allowed}"
        )
        return MeshGateOutcome(
            run_dir=run_dir,
            mesh_file=mesh_file,
            mesh_groups_file=mesh_groups_file,
            mesh_quality_file=mesh_quality_file,
            preview_png=preview_png,
            decision=decision,
            auto_check_passed=auto_check_passed,
            next_stage_allowed=next_stage_allowed,
            checks=checks,
            transcript_path=transcript_path,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_auto_checks(
        self,
        *,
        mesh_file: Path,
        mesh_groups_file: Path | None,
        mesh_quality_file: Path | None,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        mesh_check = self._diag.check_mesh_file(mesh_file)
        checks.append(
            {
                "name": mesh_check.check_name,
                "passed": mesh_check.passed,
                "layer": mesh_check.layer,
                "message": mesh_check.message,
                "suggestion": mesh_check.suggestion,
            }
        )

        if mesh_groups_file is None:
            checks.append(
                {
                    "name": "mesh_groups_exists",
                    "passed": False,
                    "layer": "interface",
                    "message": "mesh_groups.json not found.",
                    "suggestion": (
                        "Run mesh stage to export mesh_groups.json "
                        "before mesh review gate."
                    ),
                }
            )
        else:
            try:
                groups = json.loads(mesh_groups_file.read_text(encoding="utf-8"))
                gs = groups.get("groups", [])
                ok = isinstance(gs, list) and len(gs) > 0
                checks.append(
                    {
                        "name": "mesh_groups_non_empty",
                        "passed": ok,
                        "layer": "interface",
                        "message": "" if ok else "mesh_groups.json has no group entries.",
                        "suggestion": (
                            ""
                            if ok
                            else "Ensure physical groups are exported in mesh generation stage."
                        ),
                    }
                )
            except Exception as exc:
                checks.append(
                    {
                        "name": "mesh_groups_parseable",
                        "passed": False,
                        "layer": "interface",
                        "message": f"Failed to parse mesh_groups.json: {exc}",
                        "suggestion": "Re-generate mesh_groups.json in mesh stage.",
                    }
                )

        if mesh_quality_file is None:
            checks.append(
                {
                    "name": "mesh_quality_report_exists",
                    "passed": False,
                    "layer": "interface",
                    "message": "mesh_quality_report.json not found.",
                    "suggestion": "Run mesh stage to export mesh_quality_report.json.",
                }
            )
        else:
            try:
                report = json.loads(mesh_quality_file.read_text(encoding="utf-8"))
                overall = bool(report.get("overall_pass", False))
                checks.append(
                    {
                        "name": "mesh_quality_pass",
                        "passed": overall,
                        "layer": "interface",
                        "message": "" if overall else "mesh_quality_report overall_pass is false.",
                        "suggestion": (
                            ""
                            if overall
                            else (
                                "Adjust mesh size/refinement and regenerate "
                                "mesh until quality passes."
                            )
                        ),
                    }
                )
            except Exception as exc:
                checks.append(
                    {
                        "name": "mesh_quality_report_parseable",
                        "passed": False,
                        "layer": "interface",
                        "message": f"Failed to parse mesh_quality_report.json: {exc}",
                        "suggestion": "Re-generate mesh_quality_report.json in mesh stage.",
                    }
                )

        return checks

    def _generate_preview(
        self,
        *,
        mesh_file: Path,
        mesh_groups_file: Path | None,
        interactive_preview: bool,
    ) -> tuple[Path | None, dict[str, Any]]:
        try:
            preview_png = self._viz.visualize_mesh(
                mesh_inp_file=mesh_file,
                groups_json=mesh_groups_file,
                output_dir=mesh_file.parent,
                interactive=interactive_preview,
                save_png=True,
            )
        except Exception as exc:
            return (
                None,
                {
                    "name": "mesh_preview_generated",
                    "passed": False,
                    "layer": "runtime",
                    "message": f"Mesh preview failed: {exc}",
                    "suggestion": "Check pyvista installation and mesh.inp validity.",
                },
            )

        preview_ok = preview_png is not None and Path(preview_png).exists()
        return (
            Path(preview_png) if preview_png else None,
            {
                "name": "mesh_preview_generated",
                "passed": preview_ok,
                "layer": "runtime",
                "message": "" if preview_ok else "Mesh preview PNG was not generated.",
                "suggestion": (
                    ""
                    if preview_ok
                    else "Ensure visualization service can write output directory."
                ),
            },
        )

    @staticmethod
    def _resolve_artifact(
        run_dir: Path,
        candidates: list[str],
        required: bool = True,
    ) -> Path | None:
        for rel in candidates:
            p = run_dir / rel
            if p.exists():
                return p
        if required:
            joined = ", ".join(candidates)
            raise FileNotFoundError(f"Required mesh artifact not found under {run_dir}: {joined}")
        return None

    def _append_transcript(
        self,
        *,
        run_dir: Path,
        mesh_file: Path,
        mesh_groups_file: Path | None,
        mesh_quality_file: Path | None,
        preview_png: Path | None,
        decision: GateDecision,
        comment: str,
        edit_request: str,
        checks: list[dict[str, Any]],
        next_stage_allowed: bool,
    ) -> Path:
        transcript_path = run_dir / "review_transcript.json"
        payload = {"version": "v1", "records": []}
        if transcript_path.exists():
            try:
                payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {"version": "v1", "records": []}
        if not isinstance(payload.get("records"), list):
            payload["records"] = []

        auto_check_state = (
            "passed"
            if all(c["passed"] for c in checks if c["name"] != "mesh_preview_generated")
            else "failed"
        )
        preview_state = (
            "generated"
            if any(c["name"] == "mesh_preview_generated" and c["passed"] for c in checks)
            else "failed"
        )

        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "stage": "mesh",
            "state_machine": {
                "auto_check": auto_check_state,
                "preview": preview_state,
                "user_confirm": decision,
                "next_stage": "allowed" if next_stage_allowed else "blocked",
            },
            "auto_checks": checks,
            "preview": {
                "mesh_file": str(mesh_file),
                "mesh_groups_file": str(mesh_groups_file) if mesh_groups_file else None,
                "mesh_quality_file": str(mesh_quality_file) if mesh_quality_file else None,
                "preview_png": str(preview_png) if preview_png else None,
            },
            "user_decision": {
                "decision": decision,
                "comment": comment,
                "edit_request": edit_request,
            },
            "next_stage_allowed": next_stage_allowed,
        }
        payload["records"].append(record)
        transcript_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return transcript_path
