"""Run artifact locator with backward-compatible contract resolution (M1.9)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


RUN_CONTRACT_VERSION = "v1"


_CANDIDATES: dict[str, list[str]] = {
    "case_spec": ["case_spec.json"],
    "step": ["model.step", "02_cad/model.step"],
    "geometry_meta": ["geometry_meta.json", "02_cad/geometry_meta.json"],
    "mesh_inp": ["mesh.inp", "03_mesh/mesh.inp"],
    "mesh_groups": ["mesh_groups.json", "03_mesh/mesh_groups.json"],
    "mesh_quality": ["mesh_quality_report.json", "03_mesh/mesh_quality_report.json"],
    "analysis_model": ["analysis_model.json", "04_analysis_model/analysis_model.json"],
    "solver_job": ["solver_job.json", "05_solver_input/solver_job.json"],
    "job_inp": ["job.inp", "05_solver_input/job.inp"],
    "run_status": ["run_status.json", "06_solver/run_status.json"],
    "review_transcript": ["review_transcript.json"],
    "job_frd": ["job.frd", "06_solver/job.frd"],
    "issue_report": ["issue_report.json"],
}


@dataclass(frozen=True)
class ArtifactLocator:
    """Resolve canonical run artifacts from root or legacy stage subfolders."""

    run_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_dir", Path(self.run_dir).resolve())

    def resolve(self, key: str, *, required: bool = False) -> Path | None:
        candidates = _CANDIDATES.get(key)
        if not candidates:
            raise KeyError(f"Unknown artifact key: {key}")
        for rel in candidates:
            p = self.run_dir / rel
            if p.exists():
                return p
        if required:
            joined = ", ".join(candidates)
            raise FileNotFoundError(
                f"Required artifact '{key}' not found under {self.run_dir}: {joined}"
            )
        return None

    def resolve_many(self, keys: list[str]) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for key in keys:
            p = self.resolve(key, required=False)
            if p is not None:
                found[key] = p
        return found

    @staticmethod
    def candidates(key: str) -> list[str]:
        vals = _CANDIDATES.get(key)
        if vals is None:
            raise KeyError(f"Unknown artifact key: {key}")
        return list(vals)

    @staticmethod
    def contract_snapshot() -> dict[str, object]:
        return {
            "version": RUN_CONTRACT_VERSION,
            "artifacts": {k: list(v) for k, v in _CANDIDATES.items()},
        }
