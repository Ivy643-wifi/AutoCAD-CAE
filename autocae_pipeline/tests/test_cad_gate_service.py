"""Tests for CAD Gate service (auto_check + preview + decision transcript)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocae.backend.review.cad_gate import CadGateService


class _FakeVisualizationService:
    def visualize_cad(  # noqa: PLR0913
        self,
        step_file: Path,
        bounding_box: dict | None = None,
        output_dir: Path | None = None,
        interactive: bool = False,
        save_png: bool = True,
    ) -> Path | None:
        del step_file, bounding_box, interactive, save_png
        assert output_dir is not None
        out = output_dir / "viz_cad.png"
        out.write_bytes(b"fake_png")
        return out


def _prepare_run_dir(tmp_path: Path, include_meta: bool = True) -> Path:
    run_dir = tmp_path / "runs" / "case_demo"
    run_dir.mkdir(parents=True, exist_ok=True)

    # >=100 bytes to pass DiagnosticsValidator.check_step_file
    (run_dir / "model.step").write_bytes(b"0" * 200)

    if include_meta:
        meta = {
            "bounding_box": {
                "xmin": 0.0,
                "xmax": 200.0,
                "ymin": 0.0,
                "ymax": 25.0,
                "zmin": 0.0,
                "zmax": 2.0,
            }
        }
        (run_dir / "geometry_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return run_dir


def test_cad_gate_confirm_allows_next_stage(tmp_path: Path) -> None:
    run_dir = _prepare_run_dir(tmp_path, include_meta=True)
    gate = CadGateService(visualization_service=_FakeVisualizationService())

    outcome = gate.run_gate(run_dir=run_dir, decision="confirm")

    assert outcome.auto_check_passed is True
    assert outcome.next_stage_allowed is True
    assert outcome.preview_png is not None and outcome.preview_png.exists()
    assert outcome.transcript_path is not None and outcome.transcript_path.exists()

    transcript = json.loads(outcome.transcript_path.read_text(encoding="utf-8"))
    assert isinstance(transcript.get("records"), list)
    assert transcript["records"][-1]["user_decision"]["decision"] == "confirm"


def test_cad_gate_confirm_rejected_when_auto_check_failed(tmp_path: Path) -> None:
    run_dir = _prepare_run_dir(tmp_path, include_meta=False)
    gate = CadGateService(visualization_service=_FakeVisualizationService())

    with pytest.raises(ValueError, match="auto-check did not pass"):
        gate.run_gate(run_dir=run_dir, decision="confirm")


def test_cad_gate_edit_and_abort_append_transcript(tmp_path: Path) -> None:
    run_dir = _prepare_run_dir(tmp_path, include_meta=True)
    gate = CadGateService(visualization_service=_FakeVisualizationService())

    out_edit = gate.run_gate(
        run_dir=run_dir,
        decision="edit",
        comment="need fillet",
        edit_request="increase corner radius to 3mm",
    )
    assert out_edit.next_stage_allowed is False

    out_abort = gate.run_gate(run_dir=run_dir, decision="abort", comment="geometry invalid")
    assert out_abort.next_stage_allowed is False

    transcript = json.loads(out_abort.transcript_path.read_text(encoding="utf-8"))
    records = transcript.get("records", [])
    assert len(records) == 2
    assert records[0]["user_decision"]["decision"] == "edit"
    assert records[1]["user_decision"]["decision"] == "abort"
