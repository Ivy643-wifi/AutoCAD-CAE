"""CAD Gate service — auto_check + preview + user_confirm + transcript."""

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
class CadGateOutcome:
    """CAD Gate 执行结果。"""

    run_dir: Path
    step_file: Path
    geometry_meta_file: Path | None
    preview_png: Path | None
    decision: GateDecision
    auto_check_passed: bool
    next_stage_allowed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    transcript_path: Path | None = None


class CadGateService:
    """执行 CAD 阶段 gate：auto_check -> preview -> user_confirm -> transcript."""

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
    ) -> CadGateOutcome:
        run_dir = Path(run_dir).resolve()
        step_file = self._resolve_artifact(run_dir, ["model.step", "02_cad/model.step"])
        geometry_meta_file = self._resolve_artifact(
            run_dir,
            ["geometry_meta.json", "02_cad/geometry_meta.json"],
            required=False,
        )

        checks = self._run_auto_checks(step_file=step_file, geometry_meta_file=geometry_meta_file)
        auto_check_passed = all(c["passed"] for c in checks)

        preview_png, preview_check = self._generate_preview(
            step_file=step_file,
            geometry_meta_file=geometry_meta_file,
            interactive_preview=interactive_preview,
        )
        checks.append(preview_check)
        auto_check_passed = auto_check_passed and preview_check["passed"]

        if decision == "confirm" and not auto_check_passed:
            raise ValueError(
                "CAD auto-check did not pass. Use decision 'edit' or 'abort' before next stage."
            )

        next_stage_allowed = decision == "confirm" and auto_check_passed
        transcript_path = self._append_transcript(
            run_dir=run_dir,
            step_file=step_file,
            geometry_meta_file=geometry_meta_file,
            preview_png=preview_png,
            decision=decision,
            comment=comment,
            edit_request=edit_request,
            checks=checks,
            next_stage_allowed=next_stage_allowed,
        )

        logger.info(
            f"CAD Gate finished: decision={decision}, "
            f"auto_check_passed={auto_check_passed}, next_stage_allowed={next_stage_allowed}"
        )
        return CadGateOutcome(
            run_dir=run_dir,
            step_file=step_file,
            geometry_meta_file=geometry_meta_file,
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
        step_file: Path,
        geometry_meta_file: Path | None,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        step_check = self._diag.check_step_file(step_file)
        checks.append(
            {
                "name": step_check.check_name,
                "passed": step_check.passed,
                "layer": step_check.layer,
                "message": step_check.message,
                "suggestion": step_check.suggestion,
            }
        )

        if geometry_meta_file is None:
            checks.append(
                {
                    "name": "geometry_meta_exists",
                    "passed": False,
                    "layer": "interface",
                    "message": "geometry_meta.json not found.",
                    "suggestion": (
                        "Run CAD stage to export geometry_meta.json "
                        "before CAD review gate."
                    ),
                }
            )
            return checks

        try:
            meta = json.loads(geometry_meta_file.read_text(encoding="utf-8"))
            bbox = meta.get("bounding_box", {})
            required_keys = {"xmin", "xmax", "ymin", "ymax", "zmin", "zmax"}
            if not required_keys.issubset(bbox.keys()):
                checks.append(
                    {
                        "name": "geometry_bbox_complete",
                        "passed": False,
                        "layer": "interface",
                        "message": "bounding_box is missing required keys.",
                        "suggestion": (
                            "Ensure CAD stage writes complete "
                            "bbox in geometry_meta.json."
                        ),
                    }
                )
            else:
                dx = float(bbox["xmax"]) - float(bbox["xmin"])
                dy = float(bbox["ymax"]) - float(bbox["ymin"])
                dz = float(bbox["zmax"]) - float(bbox["zmin"])
                bbox_ok = dx > 0.0 and dy > 0.0 and dz >= 0.0
                checks.append(
                    {
                        "name": "geometry_bbox_valid",
                        "passed": bbox_ok,
                        "layer": "interface",
                        "message": (
                            ""
                            if bbox_ok
                            else f"Invalid bbox spans: dx={dx}, dy={dy}, dz={dz}"
                        ),
                        "suggestion": (
                            ""
                            if bbox_ok
                            else "Re-check CAD script and geometry export process."
                        ),
                    }
                )
        except Exception as exc:
            checks.append(
                {
                    "name": "geometry_meta_parseable",
                    "passed": False,
                    "layer": "interface",
                    "message": f"Failed to parse geometry_meta.json: {exc}",
                    "suggestion": "Re-generate geometry_meta.json in CAD stage.",
                }
            )
        return checks

    def _generate_preview(
        self,
        *,
        step_file: Path,
        geometry_meta_file: Path | None,
        interactive_preview: bool,
    ) -> tuple[Path | None, dict[str, Any]]:
        bbox: dict[str, float] = {}
        if geometry_meta_file is not None and geometry_meta_file.exists():
            try:
                meta = json.loads(geometry_meta_file.read_text(encoding="utf-8"))
                bbox = meta.get("bounding_box") or {}
            except Exception:
                bbox = {}

        try:
            preview_png = self._viz.visualize_cad(
                step_file=step_file,
                bounding_box=bbox,
                output_dir=step_file.parent,
                interactive=interactive_preview,
                save_png=True,
            )
        except Exception as exc:
            return (
                None,
                {
                    "name": "cad_preview_generated",
                    "passed": False,
                    "layer": "runtime",
                    "message": f"CAD preview failed: {exc}",
                    "suggestion": "Check cadquery/pyvista installation and STEP validity.",
                },
            )

        preview_ok = preview_png is not None and Path(preview_png).exists()
        return (
            Path(preview_png) if preview_png else None,
            {
                "name": "cad_preview_generated",
                "passed": preview_ok,
                "layer": "runtime",
                "message": "" if preview_ok else "CAD preview PNG was not generated.",
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
            raise FileNotFoundError(f"Required CAD artifact not found under {run_dir}: {joined}")
        return None

    def _append_transcript(
        self,
        *,
        run_dir: Path,
        step_file: Path,
        geometry_meta_file: Path | None,
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
            if all(c["passed"] for c in checks if c["name"] != "cad_preview_generated")
            else "failed"
        )
        preview_state = (
            "generated"
            if any(c["name"] == "cad_preview_generated" and c["passed"] for c in checks)
            else "failed"
        )

        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "stage": "cad",
            "state_machine": {
                "auto_check": auto_check_state,
                "preview": preview_state,
                "user_confirm": decision,
                "next_stage": "allowed" if next_stage_allowed else "blocked",
            },
            "auto_checks": checks,
            "preview": {
                "step_file": str(step_file),
                "geometry_meta_file": str(geometry_meta_file) if geometry_meta_file else None,
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
