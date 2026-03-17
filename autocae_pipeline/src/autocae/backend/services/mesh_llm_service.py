"""LLM-driven mesh script generation with bounded auto-repair (M1.5)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from autocae.backend.templates.cad.base import CADResult
from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import MeshGroups, MeshQualityReport


@dataclass
class MeshLLMRepairConfig:
    """Bounded retry controls required by V3."""

    max_attempts: int = 3
    failure_class_filter: tuple[str, ...] = (
        "syntax_error",
        "import_error",
        "runtime_error",
        "export_missing",
    )
    stop_conditions: tuple[str, ...] = (
        "success",
        "max_attempts_reached",
        "failure_class_not_allowed",
        "repeated_failure_limit",
    )
    repeated_failure_limit: int = 2


@dataclass
class ScriptExecutionResult:
    """Result returned by script executor."""

    success: bool
    return_code: int
    stdout: str
    stderr: str
    error_class: str
    error_message: str


@dataclass
class GeneratedScript:
    """Script text and provider metadata."""

    script_text: str
    provider_meta: dict[str, Any]


class MeshScriptProvider(Protocol):
    """Provider interface for mesh script generation/repair."""

    def generate_script(
        self,
        *,
        spec: CaseSpec,
        cad_result: CADResult,
        attempt: int,
        previous_script: str | None,
        error_context: str | None,
        output_dir: Path,
    ) -> GeneratedScript:
        """Return generated script for current attempt."""


class ScriptExecutor(Protocol):
    """Executor interface for generated script."""

    def execute(
        self,
        *,
        script_path: Path,
        output_dir: Path,
    ) -> ScriptExecutionResult:
        """Run script and classify failure."""


class RuleBasedMeshScriptProvider:
    """Offline fallback provider used when no online LLM is configured."""

    def generate_script(
        self,
        *,
        spec: CaseSpec,
        cad_result: CADResult,
        attempt: int,
        previous_script: str | None,
        error_context: str | None,
        output_dir: Path,
    ) -> GeneratedScript:
        del previous_script, error_context, output_dir

        spec_json = spec.to_json(indent=2)
        geometry_meta_json = cad_result.geometry_meta.to_json(indent=2)
        spec_json_literal = json.dumps(spec_json, ensure_ascii=False)
        geometry_meta_json_literal = json.dumps(geometry_meta_json, ensure_ascii=False)

        script = textwrap.dedent(
            f"""
            import argparse
            from pathlib import Path

            from autocae.backend.services.mesh_service import MeshService
            from autocae.backend.templates.cad.base import CADResult
            from autocae.schemas.case_spec import CaseSpec
            from autocae.schemas.mesh import GeometryMeta

            SPEC_JSON = {spec_json_literal}
            GEOMETRY_META_JSON = {geometry_meta_json_literal}

            def main() -> None:
                parser = argparse.ArgumentParser()
                parser.add_argument("--output-dir", required=True)
                args = parser.parse_args()
                output_dir = Path(args.output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)

                step_path = output_dir / "model.step"
                if not step_path.exists():
                    raise FileNotFoundError(f"Missing CAD artifact: {{step_path}}")

                spec = CaseSpec.model_validate_json(SPEC_JSON)
                geometry_meta = GeometryMeta.model_validate_json(GEOMETRY_META_JSON)
                geometry_meta.step_file = str(step_path)
                cad_result = CADResult(step_file=step_path, geometry_meta=geometry_meta)

                service = MeshService()
                service.build(spec, cad_result, output_dir)
                print("mesh_llm script completed")

            if __name__ == "__main__":
                main()
            """
        ).strip() + "\n"

        return GeneratedScript(
            script_text=script,
            provider_meta={
                "provider": "rule_based",
                "model": "offline_stub",
                "attempt": attempt,
            },
        )


class OpenAICompatibleMeshScriptProvider:
    """OpenAI-compatible provider using HTTP API (no extra SDK dependency)."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        api_base: str = "https://api.openai.com/v1",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.api_base = api_base.rstrip("/")

    def generate_script(
        self,
        *,
        spec: CaseSpec,
        cad_result: CADResult,
        attempt: int,
        previous_script: str | None,
        error_context: str | None,
        output_dir: Path,
    ) -> GeneratedScript:
        prompt = self._build_prompt(
            spec=spec,
            cad_result=cad_result,
            attempt=attempt,
            previous_script=previous_script,
            error_context=error_context,
            output_dir=output_dir,
        )
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You generate Python mesh scripts using gmsh only. "
                        "Return only executable Python code."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.api_base}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            msg = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM HTTPError: {exc.code} {msg}") from exc
        except Exception as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc

        parsed = json.loads(raw)
        content = parsed.get("choices", [{}])[0].get("message", {}).get("content", "")
        code = self._strip_markdown_code_fence(content)
        if not code.strip():
            raise RuntimeError("LLM returned empty mesh script.")
        return GeneratedScript(
            script_text=code,
            provider_meta={
                "provider": "openai_compatible",
                "model": self.model,
                "attempt": attempt,
            },
        )

    @staticmethod
    def _strip_markdown_code_fence(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            return "\n".join(lines).strip() + "\n"
        return cleaned + "\n"

    @staticmethod
    def _build_prompt(
        *,
        spec: CaseSpec,
        cad_result: CADResult,
        attempt: int,
        previous_script: str | None,
        error_context: str | None,
        output_dir: Path,
    ) -> str:
        repair_block = ""
        if previous_script and error_context:
            repair_block = (
                "\nPrevious script:\n"
                f"{previous_script}\n\n"
                "Execution error:\n"
                f"{error_context}\n"
                "Please repair the script.\n"
            )
        return textwrap.dedent(
            f"""
            Generate Python script for mesh stage attempt={attempt}.
            Case:
            - geometry_type: {spec.geometry.geometry_type.value}
            - analysis_type: {spec.analysis_type.value}
            - mesh_global_size: {spec.mesh_preferences.global_size}
            - mesh_element_type: {spec.mesh_preferences.element_type.value}
            - mesh_min_quality: {spec.mesh_preferences.min_quality}
            Constraints:
            - use gmsh only for meshing
            - script must accept '--output-dir'
            - input CAD file is output-dir/model.step
            - script must not modify output-dir/model.step or geometry_meta.json
            - write mesh.inp, mesh_groups.json, mesh_quality_report.json under output-dir
            - mesh_groups.json should include SOLID group at minimum
            CAD source path snapshot: {cad_result.step_file}
            Output directory is runtime-provided: {output_dir}
            {repair_block}
            Return only python code.
            """
        ).strip()


class SubprocessScriptExecutor:
    """Default executor using current Python runtime."""

    def __init__(self, python_executable: str | None = None) -> None:
        self.python_executable = python_executable or sys.executable

    def execute(
        self,
        *,
        script_path: Path,
        output_dir: Path,
    ) -> ScriptExecutionResult:
        cmd = [self.python_executable, str(script_path), "--output-dir", str(output_dir)]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            return ScriptExecutionResult(
                success=False,
                return_code=-1,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                error_class="runtime_error",
                error_message="Script execution timeout.",
            )
        except Exception as exc:
            return ScriptExecutionResult(
                success=False,
                return_code=-1,
                stdout="",
                stderr=str(exc),
                error_class="runtime_error",
                error_message=f"Script execution failed: {exc}",
            )

        if proc.returncode == 0:
            return ScriptExecutionResult(
                success=True,
                return_code=0,
                stdout=proc.stdout,
                stderr=proc.stderr,
                error_class="",
                error_message="",
            )

        combined = f"{proc.stdout}\n{proc.stderr}"
        err_cls = classify_failure(combined)
        return ScriptExecutionResult(
            success=False,
            return_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            error_class=err_cls,
            error_message=extract_error_message(combined),
        )


@dataclass
class MeshLLMBuildOutcome:
    """Return object for LLM mesh build service."""

    success: bool
    mesh_groups: MeshGroups | None
    mesh_quality: MeshQualityReport | None
    audit_path: Path
    issue_report_path: Path | None
    message: str


class MeshLLMBuildService:
    """Generate and repair mesh script with bounded retry and full audit."""

    def __init__(
        self,
        *,
        provider: MeshScriptProvider | None = None,
        executor: ScriptExecutor | None = None,
        config: MeshLLMRepairConfig | None = None,
    ) -> None:
        self.config = config or MeshLLMRepairConfig()
        self.provider = provider or self._auto_provider()
        self.executor = executor or SubprocessScriptExecutor()

    def build(
        self,
        *,
        spec: CaseSpec,
        cad_result: CADResult,
        output_dir: Path,
    ) -> MeshLLMBuildOutcome:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        llm_dir = output_dir / "mesh_llm"
        llm_dir.mkdir(parents=True, exist_ok=True)
        audit_path = llm_dir / "mesh_llm_repair_audit.json"

        started_at = datetime.now(timezone.utc).isoformat()
        attempts_payload: list[dict[str, Any]] = []
        error_counts: dict[str, int] = {}

        previous_script: str | None = None
        previous_error: str | None = None
        stop_reason = "max_attempts_reached"

        for attempt in range(1, self.config.max_attempts + 1):
            attempt_dir = llm_dir / f"attempt_{attempt:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            script_path = attempt_dir / "generated_mesh.py"
            exec_log_path = attempt_dir / "execution.log"

            generated: GeneratedScript | None = None
            try:
                generated = self.provider.generate_script(
                    spec=spec,
                    cad_result=cad_result,
                    attempt=attempt,
                    previous_script=previous_script,
                    error_context=previous_error,
                    output_dir=output_dir,
                )
                script_path.write_text(generated.script_text, encoding="utf-8")

                # Keep retries independent: remove only mesh artifacts, keep CAD artifacts untouched.
                self._clean_mesh_artifacts(output_dir)
                run_result = self.executor.execute(script_path=script_path, output_dir=output_dir)
            except Exception as exc:
                run_result = ScriptExecutionResult(
                    success=False,
                    return_code=-1,
                    stdout="",
                    stderr=str(exc),
                    error_class="runtime_error",
                    error_message=f"script_provider_error: {exc}",
                )
            exec_log_path.write_text(
                f"[stdout]\n{run_result.stdout}\n\n[stderr]\n{run_result.stderr}\n",
                encoding="utf-8",
            )

            mesh_path = output_dir / "mesh.inp"
            groups_path = output_dir / "mesh_groups.json"
            quality_path = output_dir / "mesh_quality_report.json"
            export_ok = mesh_path.exists() and groups_path.exists() and quality_path.exists()
            if run_result.success and not export_ok:
                run_result = ScriptExecutionResult(
                    success=False,
                    return_code=0,
                    stdout=run_result.stdout,
                    stderr=run_result.stderr,
                    error_class="export_missing",
                    error_message=(
                        "Script executed but one or more mesh artifacts are missing: "
                        "mesh.inp, mesh_groups.json, mesh_quality_report.json."
                    ),
                )

            attempt_payload = {
                "attempt": attempt,
                "input_summary": {
                    "case_id": spec.metadata.case_id,
                    "geometry_type": spec.geometry.geometry_type.value,
                    "analysis_type": spec.analysis_type.value,
                    "cad_step_file": str(cad_result.step_file),
                },
                "script_version": f"v{attempt}",
                "script_path": str(script_path),
                "provider_meta": generated.provider_meta if generated else {"provider_error": True},
                "execution_log_path": str(exec_log_path),
                "error_class": run_result.error_class,
                "error_message": run_result.error_message,
                "repair_action": (
                    "initial_generation"
                    if attempt == 1
                    else f"repair_from_{previous_error or 'unknown_error'}"
                ),
                "round_result": "success" if run_result.success else "failed",
            }
            attempts_payload.append(attempt_payload)

            if run_result.success:
                stop_reason = "success"
                self._write_audit(
                    path=audit_path,
                    spec=spec,
                    attempts=attempts_payload,
                    status="success",
                    stop_reason=stop_reason,
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                )
                logger.info(
                    f"Mesh LLM build success in {attempt} attempt(s). audit={audit_path}"
                )
                return MeshLLMBuildOutcome(
                    success=True,
                    mesh_groups=MeshGroups.from_json(str(groups_path)),
                    mesh_quality=MeshQualityReport.from_json(str(quality_path)),
                    audit_path=audit_path,
                    issue_report_path=None,
                    message=f"Mesh LLM build completed (attempts={attempt}).",
                )

            err_class = run_result.error_class or "runtime_error"
            error_counts[err_class] = error_counts.get(err_class, 0) + 1

            if err_class not in self.config.failure_class_filter:
                stop_reason = "failure_class_not_allowed"
                break
            if error_counts[err_class] >= self.config.repeated_failure_limit:
                stop_reason = "repeated_failure_limit"
                break

            previous_script = generated.script_text if generated else None
            previous_error = f"{err_class}: {run_result.error_message}"

        self._write_audit(
            path=audit_path,
            spec=spec,
            attempts=attempts_payload,
            status="failed",
            stop_reason=stop_reason,
            started_at=started_at,
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
        issue_path = self._write_issue_report(
            output_dir=output_dir,
            stop_reason=stop_reason,
            attempts=attempts_payload,
        )
        logger.error(f"Mesh LLM build failed: stop_reason={stop_reason}, audit={audit_path}")
        return MeshLLMBuildOutcome(
            success=False,
            mesh_groups=None,
            mesh_quality=None,
            audit_path=audit_path,
            issue_report_path=issue_path,
            message=f"Mesh LLM build failed: {stop_reason}",
        )

    @staticmethod
    def _clean_mesh_artifacts(output_dir: Path) -> None:
        for name in ("mesh.inp", "mesh.inp.bak", "mesh_groups.json", "mesh_quality_report.json"):
            path = output_dir / name
            if path.exists():
                path.unlink()

    def _auto_provider(self) -> MeshScriptProvider:
        api_key = os.getenv("AUTOCAE_LLM_API_KEY")
        if not api_key:
            return RuleBasedMeshScriptProvider()
        model = os.getenv("AUTOCAE_LLM_MODEL", "gpt-4o-mini")
        base = os.getenv("AUTOCAE_LLM_API_BASE", "https://api.openai.com/v1")
        return OpenAICompatibleMeshScriptProvider(api_key=api_key, model=model, api_base=base)

    def _write_audit(
        self,
        *,
        path: Path,
        spec: CaseSpec,
        attempts: list[dict[str, Any]],
        status: str,
        stop_reason: str,
        started_at: str,
        ended_at: str,
    ) -> dict[str, Any]:
        payload = {
            "stage": "mesh_llm",
            "status": status,
            "stop_reason": stop_reason,
            "started_at_utc": started_at,
            "ended_at_utc": ended_at,
            "config": asdict(self.config),
            "input_summary": {
                "case_id": spec.metadata.case_id,
                "case_name": spec.metadata.case_name,
                "geometry_type": spec.geometry.geometry_type.value,
                "analysis_type": spec.analysis_type.value,
            },
            "attempts": attempts,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    @staticmethod
    def _write_issue_report(
        *,
        output_dir: Path,
        stop_reason: str,
        attempts: list[dict[str, Any]],
    ) -> Path:
        last = attempts[-1] if attempts else {}
        err_class = str(last.get("error_class", "runtime_error"))
        err_msg = str(last.get("error_message", "unknown error"))
        report = {
            "error_stage": "mesh_llm",
            "error_class": err_class,
            "error_message": err_msg,
            "root_cause_hint": root_cause_hint(err_class),
            "remediation_hint": remediation_hint(err_class, stop_reason),
            "stop_reason": stop_reason,
        }
        issue_path = output_dir / "mesh_llm_issue_report.json"
        issue_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return issue_path


def classify_failure(log_text: str) -> str:
    text = log_text.lower()
    if "syntaxerror" in text:
        return "syntax_error"
    if "modulenotfounderror" in text or "importerror" in text:
        return "import_error"
    if "filenotfounderror" in text:
        return "file_not_found"
    return "runtime_error"


def extract_error_message(log_text: str) -> str:
    lines = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
    if not lines:
        return "unknown runtime error"
    return lines[-1][:300]


def root_cause_hint(error_class: str) -> str:
    mapping = {
        "syntax_error": "Generated script has Python syntax issues.",
        "import_error": "Runtime environment is missing required Python modules.",
        "export_missing": "Script did not write required mesh artifacts.",
        "file_not_found": "Expected file path in script is invalid.",
        "runtime_error": "Script execution failed with runtime exception.",
    }
    return mapping.get(error_class, "Unknown mesh script failure.")


def remediation_hint(error_class: str, stop_reason: str) -> str:
    if stop_reason == "failure_class_not_allowed":
        return "Error class is out of repair scope; manual intervention required."
    if stop_reason == "repeated_failure_limit":
        return "Same failure repeated. Inspect audit log and refine prompt/constraints."

    mapping = {
        "syntax_error": "Fix script syntax and re-run with bounded retry.",
        "import_error": "Install missing dependency (gmsh) and re-run.",
        "export_missing": "Ensure script writes mesh.inp, mesh_groups.json, mesh_quality_report.json.",
        "file_not_found": "Check output path handling and file permissions.",
        "runtime_error": "Inspect execution log, then regenerate script with error context.",
    }
    return mapping.get(error_class, "Inspect mesh_llm_repair_audit.json for details.")
