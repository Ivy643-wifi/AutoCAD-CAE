"""Environment doctor checks for M1.8."""

from __future__ import annotations

import hashlib
import importlib.util
import locale
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


CheckStatus = Literal["pass", "warn", "fail"]


@dataclass
class DoctorCheck:
    """Single doctor check result."""

    name: str
    status: CheckStatus
    message: str
    remediation: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    """Aggregated doctor report."""

    created_at_utc: str
    project_root: Path
    runs_dir: Path
    manifest_path: Path | None
    checks: list[DoctorCheck]

    @property
    def has_failures(self) -> bool:
        return any(c.status == "fail" for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.status == "warn" for c in self.checks)

    @property
    def summary_status(self) -> str:
        if self.has_failures:
            return "fail"
        if self.has_warnings:
            return "warn"
        return "pass"


class DoctorService:
    """Run environment checks with actionable remediation hints."""

    _REQUIRED_MODULES: dict[str, str] = {
        "pydantic": "pydantic",
        "yaml": "pyyaml",
        "numpy": "numpy",
        "scipy": "scipy",
        "matplotlib": "matplotlib",
        "cadquery": "cadquery",
        "gmsh": "gmsh",
        "pyvista": "pyvista",
        "rich": "rich",
        "loguru": "loguru",
        "typer": "typer",
    }

    def run(
        self,
        *,
        project_root: Path,
        runs_dir: Path,
        manifest_path: Path | None = None,
    ) -> DoctorReport:
        project_root = Path(project_root).resolve()
        runs_dir = Path(runs_dir)
        if not runs_dir.is_absolute():
            runs_dir = (project_root / runs_dir).resolve()
        manifest = self._resolve_manifest_path(project_root=project_root, manifest_path=manifest_path)

        checks = [
            self._check_python_version(),
            self._check_python_dependencies(),
            self._check_ccx_executable(),
            self._check_write_permission(path=runs_dir, check_name="runs_dir_writeable"),
            self._check_write_permission(path=project_root, check_name="project_root_writeable"),
            self._check_encoding(),
            self._check_manifest(manifest),
        ]

        return DoctorReport(
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            project_root=project_root,
            runs_dir=runs_dir,
            manifest_path=manifest,
            checks=checks,
        )

    @staticmethod
    def _resolve_manifest_path(*, project_root: Path, manifest_path: Path | None) -> Path | None:
        if manifest_path is not None:
            p = Path(manifest_path)
            if not p.is_absolute():
                p = (project_root / p).resolve()
            return p

        candidates = [
            project_root / "tools" / "manifest.yaml",
            project_root.parent / "tools" / "manifest.yaml",
        ]
        for p in candidates:
            if p.exists():
                return p
        return candidates[0]

    @staticmethod
    def _check_python_version() -> DoctorCheck:
        version = sys.version_info
        passed = (version.major, version.minor) >= (3, 10)
        msg = f"Python {version.major}.{version.minor}.{version.micro}"
        if passed:
            return DoctorCheck(
                name="python_version",
                status="pass",
                message=f"{msg} meets >=3.10 requirement.",
            )
        return DoctorCheck(
            name="python_version",
            status="fail",
            message=f"{msg} is below required version >=3.10.",
            remediation="Install Python 3.10+ and recreate virtual environment.",
        )

    def _check_python_dependencies(self) -> DoctorCheck:
        missing: list[str] = []
        for module_name, package_name in self._REQUIRED_MODULES.items():
            if importlib.util.find_spec(module_name) is None:
                missing.append(package_name)
        if not missing:
            return DoctorCheck(
                name="python_dependencies",
                status="pass",
                message="All required Python modules are importable.",
            )
        return DoctorCheck(
            name="python_dependencies",
            status="fail",
            message=f"Missing Python packages: {', '.join(sorted(missing))}",
            remediation="Run `python -m pip install -e .` or install missing packages and retry.",
            details={"missing": sorted(missing)},
        )

    @staticmethod
    def _check_ccx_executable() -> DoctorCheck:
        env_ccx = os.environ.get("CCX_PATH", "").strip()
        if env_ccx:
            ccx_path = Path(env_ccx)
            if ccx_path.exists() and ccx_path.is_file():
                return DoctorCheck(
                    name="ccx_executable",
                    status="pass",
                    message=f"CCX_PATH points to executable: {ccx_path}",
                    details={"ccx_path": str(ccx_path)},
                )
            return DoctorCheck(
                name="ccx_executable",
                status="fail",
                message=f"CCX_PATH is set but invalid: {env_ccx}",
                remediation="Set CCX_PATH to a valid ccx executable path.",
                details={"ccx_path": env_ccx},
            )

        found = shutil.which("ccx") or shutil.which("ccx.exe")
        if found:
            return DoctorCheck(
                name="ccx_executable",
                status="pass",
                message=f"ccx executable found in PATH: {found}",
                details={"ccx_path": found},
            )
        return DoctorCheck(
            name="ccx_executable",
            status="fail",
            message="No ccx executable found (CCX_PATH and PATH both unavailable).",
            remediation=(
                "Install CalculiX, then set CCX_PATH "
                "(for example: setx CCX_PATH \"D:\\...\\ccx.exe\")."
            ),
        )

    @staticmethod
    def _check_write_permission(*, path: Path, check_name: str) -> DoctorCheck:
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path,
                prefix="autocae_doctor_",
                suffix=".tmp",
                delete=False,
            ) as f:
                f.write("doctor_write_check")
                temp_path = Path(f.name)
            temp_path.unlink(missing_ok=True)
            return DoctorCheck(
                name=check_name,
                status="pass",
                message=f"Write permission OK: {path}",
            )
        except Exception as exc:
            return DoctorCheck(
                name=check_name,
                status="fail",
                message=f"Write permission failed: {path} ({exc})",
                remediation="Ensure directory exists and current user has write permission.",
            )

    @staticmethod
    def _check_encoding() -> DoctorCheck:
        stdout_enc = (sys.stdout.encoding or "").lower()
        fs_enc = (sys.getfilesystemencoding() or "").lower()
        pref_enc = (locale.getpreferredencoding(False) or "").lower()
        utf8_like = any("utf-8" in enc for enc in [stdout_enc, fs_enc, pref_enc])
        if utf8_like:
            return DoctorCheck(
                name="encoding",
                status="pass",
                message=(
                    f"Encoding looks usable (stdout={stdout_enc or 'n/a'}, "
                    f"filesystem={fs_enc or 'n/a'}, preferred={pref_enc or 'n/a'})."
                ),
            )
        return DoctorCheck(
            name="encoding",
            status="warn",
            message=(
                f"Non-UTF8 encoding detected (stdout={stdout_enc or 'n/a'}, "
                f"filesystem={fs_enc or 'n/a'}, preferred={pref_enc or 'n/a'})."
            ),
            remediation="Use UTF-8 console (PowerShell: `chcp 65001`) to avoid Unicode output issues.",
        )

    def _check_manifest(self, manifest_path: Path | None) -> DoctorCheck:
        if manifest_path is None:
            return DoctorCheck(
                name="tools_manifest",
                status="fail",
                message="tools/manifest.yaml path cannot be resolved.",
                remediation="Create tools/manifest.yaml and rerun doctor.",
            )
        if not manifest_path.exists():
            return DoctorCheck(
                name="tools_manifest",
                status="fail",
                message=f"Manifest file not found: {manifest_path}",
                remediation="Create tools/manifest.yaml with artifact path and sha256 entries.",
            )

        try:
            import yaml
        except Exception as exc:
            return DoctorCheck(
                name="tools_manifest",
                status="fail",
                message=f"pyyaml is not available for parsing manifest: {exc}",
                remediation="Install pyyaml and rerun doctor.",
            )

        try:
            payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            return DoctorCheck(
                name="tools_manifest",
                status="fail",
                message=f"Manifest parse failed: {exc}",
                remediation="Fix YAML syntax in tools/manifest.yaml.",
            )

        artifacts = payload.get("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) == 0:
            return DoctorCheck(
                name="tools_manifest",
                status="fail",
                message="Manifest has no artifacts entries.",
                remediation="Add at least one artifact entry with path and sha256.",
            )

        failures: list[str] = []
        warnings: list[str] = []
        verified = 0
        for item in artifacts:
            if not isinstance(item, dict):
                failures.append("invalid_artifact_entry")
                continue

            name = str(item.get("name", "unnamed"))
            rel_path = str(item.get("path", "")).strip()
            expected = str(item.get("sha256", "")).strip().lower()
            required = bool(item.get("required", True))
            if not rel_path or not expected:
                failures.append(f"{name}:missing_path_or_sha256")
                continue

            artifact_path = (manifest_path.parent / rel_path).resolve()
            if not artifact_path.exists():
                if required:
                    failures.append(f"{name}:missing_file")
                else:
                    warnings.append(f"{name}:missing_optional_file")
                continue

            actual = self._file_sha256(artifact_path)
            if actual != expected:
                failures.append(f"{name}:sha256_mismatch")
                continue
            verified += 1

        if failures:
            return DoctorCheck(
                name="tools_manifest",
                status="fail",
                message=f"Manifest verification failed: {', '.join(failures)}",
                remediation="Update incorrect sha256/path in tools/manifest.yaml or replace corrupted files.",
                details={"verified_count": verified, "warnings": warnings, "failures": failures},
            )
        if warnings:
            return DoctorCheck(
                name="tools_manifest",
                status="warn",
                message=f"Manifest verification passed with warnings: {', '.join(warnings)}",
                remediation="Install optional tools if needed by your workflow.",
                details={"verified_count": verified, "warnings": warnings},
            )
        return DoctorCheck(
            name="tools_manifest",
            status="pass",
            message=f"Manifest verification passed ({verified} artifact(s) verified).",
            details={"verified_count": verified},
        )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest().lower()
