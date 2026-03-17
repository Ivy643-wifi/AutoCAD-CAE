"""Review gate guards used before entering downstream stages."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from autocae.backend.orchestrator.artifact_locator import ArtifactLocator


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
    transcript_path = ArtifactLocator(run_dir).resolve("review_transcript", required=False)
    if transcript_path is None or not transcript_path.exists():
        raise error_cls(missing_transcript_hint)

    try:
        # Accept UTF-8 files with/without BOM to avoid editor-specific decode failures.
        transcript = json.loads(transcript_path.read_text(encoding="utf-8-sig"))
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

    _ensure_gate_record_is_fresh(
        run_dir=run_dir,
        stage=stage,
        latest_record=latest,
        error_cls=error_cls,
    )

    return latest


def _ensure_gate_record_is_fresh(
    *,
    run_dir: Path,
    stage: str,
    latest_record: dict[str, Any],
    error_cls: type[RuntimeError],
) -> None:
    timestamp = latest_record.get("timestamp_utc")
    if not isinstance(timestamp, str):
        raise error_cls(
            f"{stage.capitalize()} gate record timestamp is missing/invalid; please re-run review."
        )
    normalized_ts = timestamp.strip()
    if normalized_ts.endswith("Z"):
        normalized_ts = normalized_ts[:-1] + "+00:00"
    # Python 3.10 datetime.fromisoformat only accepts up to 6 fractional digits.
    normalized_ts = re.sub(
        r"(\.\d{6})\d+([+-]\d{2}:\d{2})$",
        r"\1\2",
        normalized_ts,
    )
    try:
        reviewed_at = datetime.fromisoformat(normalized_ts)
    except ValueError as exc:
        raise error_cls(
            f"{stage.capitalize()} gate timestamp parse failed: {timestamp}"
        ) from exc
    if reviewed_at.tzinfo is None:
        reviewed_at = reviewed_at.replace(tzinfo=timezone.utc)
    else:
        reviewed_at = reviewed_at.astimezone(timezone.utc)

    artifact_mtime = _latest_artifact_mtime(run_dir=run_dir, stage=stage, error_cls=error_cls)
    # Allow tiny clock/fs jitter while still rejecting stale review records.
    if reviewed_at + timedelta(seconds=1) < artifact_mtime:
        raise error_cls(
            f"{stage.capitalize()} gate record is stale "
            f"(reviewed_at={reviewed_at.isoformat()}, artifact_updated_at={artifact_mtime.isoformat()}). "
            f"Please re-run 'autocae preview {stage} <run_dir>' and confirm again."
        )


def _latest_artifact_mtime(
    *,
    run_dir: Path,
    stage: str,
    error_cls: type[RuntimeError],
) -> datetime:
    locator = ArtifactLocator(run_dir)
    if stage == "cad":
        artifact_paths = list(
            locator.resolve_many(["step", "geometry_meta"]).values()
        )
    elif stage == "mesh":
        artifact_paths = list(
            locator.resolve_many(["mesh_inp", "mesh_groups", "mesh_quality"]).values()
        )
    else:
        raise error_cls(f"Unsupported gate stage for freshness check: {stage}")

    if not artifact_paths:
        raise error_cls(
            f"No {stage} artifacts found for freshness check under {run_dir}."
        )

    latest_epoch = max(path.stat().st_mtime for path in artifact_paths)
    return datetime.fromtimestamp(latest_epoch, tz=timezone.utc)
