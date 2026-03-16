"""Review gate guards used before entering downstream stages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class MeshGateError(RuntimeError):
    """Raised when mesh gate is missing or not passed."""


class CadGateError(RuntimeError):
    """Raised when CAD gate is missing or not passed."""


def ensure_cad_gate_passed(run_dir: Path) -> dict[str, Any]:
    """Ensure CAD gate is confirmed and allowed before mesh stage."""
    latest = _ensure_stage_gate_passed(
        run_dir=run_dir,
        stage="cad",
        missing_transcript_hint=(
            "CAD gate transcript not found. Run 'autocae preview cad <run_dir>' first."
        ),
        missing_stage_hint=(
            "No CAD gate record found in review_transcript.json. "
            "Run 'autocae preview cad <run_dir>' before mesh stage."
        ),
        error_cls=CadGateError,
    )
    return latest


def ensure_mesh_gate_passed(run_dir: Path) -> dict[str, Any]:
    """Ensure mesh gate is confirmed and allowed before solver stage.

    Returns the latest mesh gate record when passed.
    """
    latest = _ensure_stage_gate_passed(
        run_dir=run_dir,
        stage="mesh",
        missing_transcript_hint=(
            "Mesh gate transcript not found. Run 'autocae preview mesh <run_dir>' first."
        ),
        missing_stage_hint=(
            "No mesh gate record found in review_transcript.json. "
            "Run 'autocae preview mesh <run_dir>' before solve."
        ),
        error_cls=MeshGateError,
    )
    return latest


def _ensure_stage_gate_passed(
    *,
    run_dir: Path,
    stage: str,
    missing_transcript_hint: str,
    missing_stage_hint: str,
    error_cls: type[RuntimeError],
) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    transcript_path = run_dir / "review_transcript.json"
    if not transcript_path.exists():
        raise error_cls(missing_transcript_hint)

    try:
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise error_cls(f"review_transcript.json parse failed: {exc}") from exc

    records = transcript.get("records")
    if not isinstance(records, list):
        raise error_cls("review_transcript.json has invalid 'records' format.")

    stage_records = [r for r in records if isinstance(r, dict) and r.get("stage") == stage]
    if not stage_records:
        raise error_cls(missing_stage_hint)

    latest = stage_records[-1]
    user_decision = str(latest.get("user_decision", {}).get("decision", "")).lower()
    next_allowed = bool(latest.get("next_stage_allowed", False))
    if user_decision != "confirm" or not next_allowed:
        raise error_cls(
            f"{stage.capitalize()} gate not passed. Latest decision is "
            f"'{user_decision or 'unknown'}' (next_stage_allowed={next_allowed})."
        )

    return latest
