"""Intake service — V3 检索优先路由（Template/Project Case -> CaseSpec）。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from autocae.backend.templates.registry import CaseTemplate, TemplateRegistry
from autocae.schemas.case_spec import (
    AnalysisType,
    BoundaryCondition,
    BoundaryType,
    CaseSpec,
    CaseSpecMetadata,
    Geometry,
    GeometryType,
    LayupLayer,
    Load,
    LoadType,
    Material,
    MeshPreferences,
    Topology,
)

CandidateSource = Literal["template", "project_case"]


_DEFAULT_DIMS: dict[GeometryType, tuple[float, float, float]] = {
    GeometryType.FLAT_PLATE: (200.0, 25.0, 2.0),
    GeometryType.OPEN_HOLE_PLATE: (300.0, 36.0, 2.0),
    GeometryType.NOTCHED_PLATE: (220.0, 30.0, 2.0),
    GeometryType.CYLINDRICAL_SHELL: (500.0, 100.0, 3.0),
    GeometryType.PRESSURE_SHELL: (500.0, 100.0, 3.0),
    GeometryType.LAMINATED_BEAM: (500.0, 25.0, 10.0),
    GeometryType.STRINGER_STIFFENED_PANEL: (600.0, 300.0, 2.5),
    GeometryType.SANDWICH_PLATE: (400.0, 100.0, 24.0),
    GeometryType.BOLTED_LAP_JOINT: (200.0, 40.0, 4.0),
}

_GEOMETRY_TO_TOPOLOGY: dict[GeometryType, Topology] = {
    GeometryType.FLAT_PLATE: Topology.LAMINATE,
    GeometryType.OPEN_HOLE_PLATE: Topology.LAMINATE,
    GeometryType.NOTCHED_PLATE: Topology.LAMINATE,
    GeometryType.CYLINDRICAL_SHELL: Topology.SHELL,
    GeometryType.PRESSURE_SHELL: Topology.SHELL,
    GeometryType.LAMINATED_BEAM: Topology.BEAM,
    GeometryType.STRINGER_STIFFENED_PANEL: Topology.PANEL,
    GeometryType.SANDWICH_PLATE: Topology.SANDWICH,
    GeometryType.BOLTED_LAP_JOINT: Topology.JOINT,
}

_GEOMETRY_KEYWORDS: tuple[tuple[GeometryType, tuple[str, ...]], ...] = (
    (GeometryType.OPEN_HOLE_PLATE, ("open hole", "hole plate", "oht", "ohc")),
    (GeometryType.CYLINDRICAL_SHELL, ("cylindrical shell", "cylinder shell", "shell")),
    (GeometryType.LAMINATED_BEAM, ("laminated beam", "beam")),
    (GeometryType.STRINGER_STIFFENED_PANEL, ("stringer", "stiffened panel", "panel")),
    (GeometryType.SANDWICH_PLATE, ("sandwich", "core plate", "夹芯")),
    (GeometryType.BOLTED_LAP_JOINT, ("bolted", "lap joint", "joint")),
    (GeometryType.FLAT_PLATE, ("flat plate", "plate", "平板")),
)

_ANALYSIS_KEYWORDS: tuple[tuple[AnalysisType, tuple[str, ...]], ...] = (
    (AnalysisType.STATIC_TENSION, ("tension", "拉伸")),
    (AnalysisType.STATIC_COMPRESSION, ("compression", "压缩")),
    (AnalysisType.BUCKLING, ("buckling", "屈曲")),
    (AnalysisType.BENDING, ("bending", "弯曲")),
    (AnalysisType.MODAL, ("modal", "频率", "模态")),
    (AnalysisType.PRESSURE, ("pressure", "内压", "外压")),
    (AnalysisType.SHEAR, ("shear", "剪切")),
    (AnalysisType.TORSION, ("torsion", "扭转")),
    (AnalysisType.THERMAL, ("thermal", "热")),
    (AnalysisType.IMPACT, ("impact", "冲击")),
    (AnalysisType.FATIGUE, ("fatigue", "疲劳")),
)

_SUPPORTED_ANALYSIS_BY_GEOMETRY: dict[GeometryType, tuple[AnalysisType, ...]] = {
    GeometryType.FLAT_PLATE: (
        AnalysisType.STATIC_TENSION,
        AnalysisType.STATIC_COMPRESSION,
        AnalysisType.BENDING,
        AnalysisType.BUCKLING,
        AnalysisType.MODAL,
    ),
    GeometryType.OPEN_HOLE_PLATE: (
        AnalysisType.STATIC_TENSION,
        AnalysisType.STATIC_COMPRESSION,
    ),
    GeometryType.NOTCHED_PLATE: (
        AnalysisType.STATIC_TENSION,
        AnalysisType.STATIC_COMPRESSION,
    ),
    GeometryType.CYLINDRICAL_SHELL: (
        AnalysisType.PRESSURE,
        AnalysisType.BUCKLING,
        AnalysisType.MODAL,
    ),
    GeometryType.PRESSURE_SHELL: (
        AnalysisType.PRESSURE,
        AnalysisType.BUCKLING,
    ),
    GeometryType.LAMINATED_BEAM: (
        AnalysisType.BENDING,
        AnalysisType.TORSION,
        AnalysisType.MODAL,
    ),
    GeometryType.STRINGER_STIFFENED_PANEL: (
        AnalysisType.BUCKLING,
        AnalysisType.STATIC_TENSION,
        AnalysisType.STATIC_COMPRESSION,
    ),
    GeometryType.SANDWICH_PLATE: (
        AnalysisType.BENDING,
        AnalysisType.SHEAR,
        AnalysisType.BUCKLING,
    ),
    GeometryType.BOLTED_LAP_JOINT: (
        AnalysisType.STATIC_TENSION,
        AnalysisType.SHEAR,
    ),
}


@dataclass
class RetrievalCandidate:
    """单条检索候选。"""

    source: CandidateSource
    candidate_id: str
    confidence: float
    reason: str
    geometry_type: str
    analysis_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IntakeOutcome:
    """intake 执行产物。"""

    case_spec: CaseSpec
    run_dir: Path
    case_spec_path: Path
    intake_decision_path: Path
    decision: dict[str, Any]


class IntakeService:
    """V3 Intake 服务：先检索，后生成。"""

    def __init__(self, template_registry: TemplateRegistry | None = None) -> None:
        self._template_registry = template_registry or TemplateRegistry()

    def intake(
        self,
        *,
        text: str | None = None,
        step_file: Path | None = None,
        image_file: Path | None = None,
        runs_dir: Path = Path("runs"),
        project_case_library: Path = Path("project_case_library"),
        min_reuse_confidence: float = 0.75,
    ) -> IntakeOutcome:
        """执行 intake 路由并输出 case_spec.json + intake_decision.json。"""
        normalized = self._normalize_input(
            text=text,
            step_file=step_file,
            image_file=image_file,
        )

        candidates: list[RetrievalCandidate] = []
        candidates.extend(self._retrieve_templates(normalized))
        candidates.extend(self._retrieve_project_cases(normalized, project_case_library))
        candidates.sort(key=lambda item: item.confidence, reverse=True)

        selected = candidates[0] if candidates else None
        should_reuse = selected is not None and selected.confidence >= min_reuse_confidence

        if should_reuse and selected is not None:
            if selected.source == "template":
                template = self._template_registry.get(selected.candidate_id)
                if template is None:
                    raise KeyError(f"Template not found during intake reuse: {selected.candidate_id}")
                case_spec = self._build_case_spec_from_template(template, normalized)
            else:
                source_case_path = Path(str(selected.metadata.get("case_spec_path", "")))
                case_spec = self._build_case_spec_from_project_case(
                    source_case_path=source_case_path,
                    normalized=normalized,
                )
            final_path = "reuse"
            decision_reason = (
                f"Top candidate confidence={selected.confidence:.3f} "
                f">= threshold={min_reuse_confidence:.3f}"
            )
        else:
            case_spec = self._build_generated_case_spec(normalized)
            final_path = "generate"
            if selected is None:
                decision_reason = "No retrieval candidate found from template/project case libraries."
            else:
                decision_reason = (
                    f"Top candidate confidence={selected.confidence:.3f} "
                    f"< threshold={min_reuse_confidence:.3f}; fallback to generation."
                )
        if normalized.get("analysis_adjustment"):
            decision_reason = (
                f"{decision_reason} | analysis_adjusted={normalized['analysis_adjustment']}"
            )

        run_dir = Path(runs_dir) / case_spec.metadata.case_id
        run_dir.mkdir(parents=True, exist_ok=True)

        case_spec_path = run_dir / "case_spec.json"
        case_spec_path.write_text(case_spec.to_json(), encoding="utf-8")

        decision = self._build_decision_payload(
            normalized=normalized,
            candidates=candidates,
            final_path=final_path,
            selected=selected if should_reuse else None,
            decision_reason=decision_reason,
            case_spec=case_spec,
            project_case_library=project_case_library,
        )
        intake_decision_path = run_dir / "intake_decision.json"
        import json

        intake_decision_path.write_text(
            json.dumps(decision, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.info(f"Intake completed: path={final_path}, case_id={case_spec.metadata.case_id}")
        logger.info(f"  case_spec.json -> {case_spec_path}")
        logger.info(f"  intake_decision.json -> {intake_decision_path}")

        return IntakeOutcome(
            case_spec=case_spec,
            run_dir=run_dir,
            case_spec_path=case_spec_path,
            intake_decision_path=intake_decision_path,
            decision=decision,
        )

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def _normalize_input(
        self,
        *,
        text: str | None,
        step_file: Path | None,
        image_file: Path | None,
    ) -> dict[str, Any]:
        has_text = bool(text and text.strip())
        has_step = step_file is not None
        has_image = image_file is not None
        if not (has_text or has_step or has_image):
            raise ValueError("At least one input is required: --text or --step-file (or --image-file).")

        if step_file is not None and not step_file.exists():
            raise FileNotFoundError(f"STEP file not found: {step_file}")
        if image_file is not None and not image_file.exists():
            raise FileNotFoundError(f"Image file not found: {image_file}")

        raw_text = (text or "").strip()
        text_lower = raw_text.lower()
        inferred_geometry = self._infer_geometry_type(raw_text, step_file)
        inferred_analysis = self._infer_analysis_type(text_lower)
        resolved_analysis, analysis_adjustment = self._resolve_supported_analysis(
            geometry_type=inferred_geometry,
            requested_analysis=inferred_analysis,
        )

        dims_from_text = self._extract_dimensions_from_text(text_lower)
        dims: dict[str, float] = dict(dims_from_text)

        if step_file is not None:
            step_dims = self._extract_dimensions_from_step(step_file)
            dims.update({k: v for k, v in step_dims.items() if v > 0})
        explicit_dimension_keys = sorted(dims.keys())

        # Separate main dims from extra geometry params (params-first merge support)
        _MAIN_DIM_KEYS = {"length", "width", "thickness"}
        extra_dims_from_user: dict[str, float] = {
            k: v for k, v in dims.items() if k not in _MAIN_DIM_KEYS
        }

        default_dims = _DEFAULT_DIMS[inferred_geometry]
        length = float(dims.get("length", default_dims[0]))
        width = float(dims.get("width", default_dims[1]))
        thickness = float(dims.get("thickness", default_dims[2]))

        case_name = self._build_case_name(raw_text=raw_text, step_file=step_file)
        if has_text:
            input_type = "text"
        elif has_step:
            input_type = "step"
        else:
            input_type = "image"

        return {
            "input_type": input_type,
            "text": raw_text,
            "step_file": str(step_file.resolve()) if step_file is not None else None,
            "image_file": str(image_file.resolve()) if image_file is not None else None,
            "geometry_type": inferred_geometry,
            "analysis_type": inferred_analysis,
            "resolved_analysis_type": resolved_analysis,
            "analysis_adjustment": analysis_adjustment,
            "topology": _GEOMETRY_TO_TOPOLOGY[inferred_geometry],
            "length": max(length, 1e-3),
            "width": max(width, 1e-3),
            "thickness": max(thickness, 1e-3),
            "explicit_dimension_keys": explicit_dimension_keys,
            "extra_dims": extra_dims_from_user,  # user-provided extra geometry params
            "case_name": case_name,
            "normalized_text": text_lower,
            "text_tokens": self._tokenize(text_lower),
        }

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {tok for tok in re.split(r"[^0-9a-z_]+", text.lower()) if tok}

    def _infer_geometry_type(self, text: str, step_file: Path | None) -> GeometryType:
        text_lower = text.lower()
        for geometry_type, keywords in _GEOMETRY_KEYWORDS:
            if any(kw in text_lower for kw in keywords):
                return geometry_type

        if step_file is not None:
            stem = step_file.stem.lower()
            for geometry_type, keywords in _GEOMETRY_KEYWORDS:
                if any(kw.replace(" ", "_") in stem or kw in stem for kw in keywords):
                    return geometry_type
        return GeometryType.FLAT_PLATE

    def _infer_analysis_type(self, text_lower: str) -> AnalysisType:
        for analysis_type, keywords in _ANALYSIS_KEYWORDS:
            if any(kw in text_lower for kw in keywords):
                return analysis_type
        return AnalysisType.STATIC_TENSION

    @staticmethod
    def _resolve_supported_analysis(
        *,
        geometry_type: GeometryType,
        requested_analysis: AnalysisType,
    ) -> tuple[AnalysisType, str | None]:
        supported = _SUPPORTED_ANALYSIS_BY_GEOMETRY.get(geometry_type, ())
        if not supported:
            return requested_analysis, None
        if requested_analysis in supported:
            return requested_analysis, None

        fallback_order = (
            AnalysisType.STATIC_TENSION,
            AnalysisType.STATIC_COMPRESSION,
            AnalysisType.BENDING,
            AnalysisType.BUCKLING,
            AnalysisType.MODAL,
            AnalysisType.PRESSURE,
            AnalysisType.SHEAR,
            AnalysisType.TORSION,
            AnalysisType.THERMAL,
            AnalysisType.IMPACT,
            AnalysisType.FATIGUE,
        )
        for candidate in fallback_order:
            if candidate in supported:
                return (
                    candidate,
                    (
                        f"{requested_analysis.value} -> {candidate.value} "
                        f"(unsupported for geometry={geometry_type.value})"
                    ),
                )
        return requested_analysis, None

    @staticmethod
    def _extract_dimensions_from_text(text_lower: str) -> dict[str, float]:
        dims: dict[str, float] = {}
        patterns: dict[str, tuple[str, ...]] = {
            "length": (r"\blength\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", r"\bl\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)"),
            "width": (r"\bwidth\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", r"\bw\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)"),
            "thickness": (r"\bthickness\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", r"\bt\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)"),
            # extra geometry params (allow template defaults to be overridden by user text)
            "hole_diameter": (r"\bhole[_\s]?dia(?:meter)?\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",),
            "radius": (r"\bradius\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", r"\br\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)"),
            "core_thickness": (r"\bcore[_\s]?thickness\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",),
            "n_bolts": (r"\bn[_\s]?bolts?\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",),
            "bolt_diameter": (r"\bbolt[_\s]?dia(?:meter)?\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",),
            "n_stringers": (r"\bn[_\s]?stringers?\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",),
            "stringer_height": (r"\bstringer[_\s]?height\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",),
        }
        for key, regexes in patterns.items():
            for pattern in regexes:
                match = re.search(pattern, text_lower)
                if match:
                    dims[key] = float(match.group(1))
                    break
        return dims

    @staticmethod
    def _extract_dimensions_from_step(step_file: Path) -> dict[str, float]:
        try:
            import gmsh
        except Exception:
            return {}

        gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Verbosity", 0)
            gmsh.model.add("autocae_intake_step_probe")
            gmsh.model.occ.importShapes(str(step_file))
            gmsh.model.occ.synchronize()
            xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
        except Exception:
            return {}
        finally:
            gmsh.finalize()

        return {
            "length": abs(xmax - xmin),
            "width": abs(ymax - ymin),
            "thickness": abs(zmax - zmin) if abs(zmax - zmin) > 1e-6 else 1.0,
        }

    @staticmethod
    def _build_case_name(raw_text: str, step_file: Path | None) -> str:
        if raw_text:
            cleaned = re.sub(r"\s+", " ", raw_text).strip()
            return cleaned[:60]
        if step_file is not None:
            return f"step_{step_file.stem}"
        return "intake_case"

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def _retrieve_templates(self, normalized: dict[str, Any]) -> list[RetrievalCandidate]:
        target_geo = normalized["geometry_type"]
        target_analysis = normalized["analysis_type"]
        candidates: list[RetrievalCandidate] = []

        for template_id in self._template_registry.list_templates():
            template = self._template_registry.get(template_id)
            if template is None:
                continue

            score = 0.20
            reasons: list[str] = []
            if template.geometry_type == target_geo:
                score += 0.45
                reasons.append("geometry_type matched")
            if template.analysis_type == target_analysis:
                score += 0.35
                reasons.append("analysis_type matched")
            if not reasons:
                continue

            candidates.append(
                RetrievalCandidate(
                    source="template",
                    candidate_id=template.template_id,
                    confidence=min(score, 0.97),
                    reason=", ".join(reasons),
                    geometry_type=template.geometry_type.value,
                    analysis_type=template.analysis_type.value,
                    metadata={"version": template.version},
                )
            )

        return candidates

    def _retrieve_project_cases(
        self,
        normalized: dict[str, Any],
        project_case_library: Path,
    ) -> list[RetrievalCandidate]:
        lib_dir = Path(project_case_library)
        if not lib_dir.exists():
            return []

        target_geo = normalized["geometry_type"]
        target_analysis = normalized["analysis_type"]
        candidates: list[RetrievalCandidate] = []

        for case_path in lib_dir.rglob("case_spec.json"):
            try:
                spec = CaseSpec.model_validate_json(case_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            score = 0.25
            reasons: list[str] = []
            if spec.geometry.geometry_type == target_geo:
                score += 0.45
                reasons.append("geometry_type matched")
            if spec.analysis_type == target_analysis:
                score += 0.35
                reasons.append("analysis_type matched")
            if not reasons:
                continue

            candidates.append(
                RetrievalCandidate(
                    source="project_case",
                    candidate_id=spec.metadata.case_id,
                    confidence=min(score, 0.99),
                    reason=", ".join(reasons),
                    geometry_type=spec.geometry.geometry_type.value,
                    analysis_type=spec.analysis_type.value,
                    metadata={"case_spec_path": str(case_path.resolve())},
                )
            )

        return candidates

    # ------------------------------------------------------------------
    # CaseSpec builders
    # ------------------------------------------------------------------

    def _build_case_spec_from_template(
        self, template: CaseTemplate, normalized: dict[str, Any]
    ) -> CaseSpec:
        geometry = self._build_geometry_from_template(template, normalized)
        topology = _GEOMETRY_TO_TOPOLOGY[template.geometry_type]
        # User's resolved analysis type takes priority; template.analysis_type is the fallback
        # (they are always the same when confidence >= threshold, but explicit is clearer)
        analysis_type = normalized["resolved_analysis_type"]
        return self._assemble_case_spec(
            topology=topology,
            geometry=geometry,
            analysis_type=analysis_type,
            template_id=template.template_id,
            case_name=normalized["case_name"],
            source="template_reuse",
            layup_angles=template.default_layup,
        )

    def _build_case_spec_from_project_case(
        self, source_case_path: Path, normalized: dict[str, Any]
    ) -> CaseSpec:
        if not source_case_path.exists():
            raise FileNotFoundError(f"Project case spec not found: {source_case_path}")

        old_spec = CaseSpec.model_validate_json(source_case_path.read_text(encoding="utf-8"))
        spec = old_spec.model_copy(deep=True)
        dims_changed = self._apply_explicit_dimension_overrides(
            geometry=spec.geometry,
            normalized=normalized,
        )
        if dims_changed and spec.topology == Topology.LAMINATE and spec.layup:
            ply_t = max(spec.geometry.thickness / len(spec.layup), 0.1)
            spec.layup = [
                layer.model_copy(update={"thickness": ply_t})
                for layer in spec.layup
            ]
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        spec.metadata = CaseSpecMetadata(
            case_name=normalized["case_name"],
            source="project_case_reuse",
            template_id=old_spec.metadata.template_id,
            notes=f"reused_from={old_spec.metadata.case_id}",
            created_at=now,
            updated_at=now,
        )
        return spec

    def _build_generated_case_spec(self, normalized: dict[str, Any]) -> CaseSpec:
        geometry_type: GeometryType = normalized["geometry_type"]
        topology = _GEOMETRY_TO_TOPOLOGY[geometry_type]
        extra = self._default_extra(geometry_type)
        extra.update(normalized.get("extra_dims", {}))  # user extra params override defaults
        geometry = Geometry(
            geometry_type=geometry_type,
            length=normalized["length"],
            width=normalized["width"],
            thickness=normalized["thickness"],
            extra=extra,
        )
        return self._assemble_case_spec(
            topology=topology,
            geometry=geometry,
            analysis_type=normalized["resolved_analysis_type"],
            template_id=None,
            case_name=normalized["case_name"],
            source="generated_from_intake",
            layup_angles=[],
        )

    def _build_geometry_from_template(
        self, template: CaseTemplate, normalized: dict[str, Any]
    ) -> Geometry:
        default_dims = _DEFAULT_DIMS[template.geometry_type]
        explicit_dims = set(normalized.get("explicit_dimension_keys", []))
        length = (
            float(normalized["length"])
            if "length" in explicit_dims
            else float(template.default_geometry.get("length", default_dims[0]))
        )
        width = (
            float(normalized["width"])
            if "width" in explicit_dims
            else float(template.default_geometry.get("width", default_dims[1]))
        )
        thickness = (
            float(normalized["thickness"])
            if "thickness" in explicit_dims
            else float(template.default_geometry.get("thickness", default_dims[2]))
        )
        # Build extra: template defaults as base, user-provided extra params override (params-first)
        extra = {
            key: float(value)
            for key, value in template.default_geometry.items()
            if key not in {"length", "width", "thickness"}
        }
        if not extra:
            extra = self._default_extra(template.geometry_type)
        user_extra = normalized.get("extra_dims", {})
        extra.update(user_extra)  # user wins over template defaults
        return Geometry(
            geometry_type=template.geometry_type,
            length=length,
            width=width,
            thickness=thickness,
            extra=extra,
        )

    @staticmethod
    def _apply_explicit_dimension_overrides(
        *,
        geometry: Geometry,
        normalized: dict[str, Any],
    ) -> bool:
        explicit_dims = set(normalized.get("explicit_dimension_keys", []))
        changed = False
        for key in ("length", "width", "thickness"):
            if key not in explicit_dims:
                continue
            new_val = float(normalized[key])
            if float(getattr(geometry, key)) != new_val:
                setattr(geometry, key, new_val)
                changed = True
        return changed

    def _assemble_case_spec(
        self,
        *,
        topology: Topology,
        geometry: Geometry,
        analysis_type: AnalysisType,
        template_id: str | None,
        case_name: str,
        source: str,
        layup_angles: list[float],
    ) -> CaseSpec:
        mesh_size = max(min(geometry.length, geometry.width) / 20.0, 1.0)
        layup = self._build_layup(topology, geometry.thickness, layup_angles)
        materials = [self._default_material(topology)]
        loads = [self._default_load(analysis_type)]
        bcs = [self._default_boundary(analysis_type)]

        return CaseSpec(
            metadata=CaseSpecMetadata(
                case_name=case_name,
                source=source,
                template_id=template_id,
            ),
            topology=topology,
            geometry=geometry,
            layup=layup,
            materials=materials,
            loads=loads,
            boundary_conditions=bcs,
            analysis_type=analysis_type,
            mesh_preferences=MeshPreferences(global_size=mesh_size),
            template_preferences={"template_id": template_id} if template_id else {},
        )

    @staticmethod
    def _default_extra(geometry_type: GeometryType) -> dict[str, float]:
        if geometry_type == GeometryType.OPEN_HOLE_PLATE:
            return {"hole_diameter": 6.0}
        if geometry_type in {GeometryType.CYLINDRICAL_SHELL, GeometryType.PRESSURE_SHELL}:
            return {"radius": 50.0}
        if geometry_type == GeometryType.STRINGER_STIFFENED_PANEL:
            return {"n_stringers": 3.0, "stringer_height": 20.0}
        if geometry_type == GeometryType.SANDWICH_PLATE:
            return {"core_thickness": 20.0}
        if geometry_type == GeometryType.BOLTED_LAP_JOINT:
            return {"n_bolts": 2.0, "bolt_diameter": 6.35}
        return {}

    @staticmethod
    def _build_layup(
        topology: Topology, thickness: float, layup_angles: list[float]
    ) -> list[LayupLayer]:
        if topology != Topology.LAMINATE:
            return []

        angles = layup_angles if layup_angles else [0.0, 45.0, -45.0, 90.0]
        ply_t = max(thickness / max(len(angles), 1), 0.1)
        return [LayupLayer(angle=float(a), thickness=ply_t, material_id="default") for a in angles]

    @staticmethod
    def _default_material(topology: Topology) -> Material:
        if topology == Topology.LAMINATE:
            return Material(
                material_id="default",
                name="Carbon_UD_Default",
                E1=135000.0,
                E2=10000.0,
                G12=5200.0,
                nu12=0.3,
            )
        return Material(
            material_id="default",
            name="Aluminium_Default",
            E=71700.0,
            nu=0.33,
        )

    @staticmethod
    def _default_load(analysis_type: AnalysisType) -> Load:
        if analysis_type == AnalysisType.STATIC_COMPRESSION:
            return Load(load_type=LoadType.COMPRESSION, magnitude=1000.0, location="LOAD_END")
        if analysis_type == AnalysisType.BENDING:
            return Load(load_type=LoadType.BENDING, magnitude=100.0, location="LOAD_END")
        if analysis_type == AnalysisType.BUCKLING:
            return Load(load_type=LoadType.COMPRESSION, magnitude=500.0, location="LOAD_END")
        if analysis_type == AnalysisType.PRESSURE:
            return Load(load_type=LoadType.PRESSURE, magnitude=1.0, location="INNER_SURFACE")
        if analysis_type == AnalysisType.SHEAR:
            return Load(load_type=LoadType.SHEAR, magnitude=300.0, location="LOAD_END")
        if analysis_type == AnalysisType.TORSION:
            return Load(load_type=LoadType.TORSION, magnitude=300.0, location="LOAD_END")
        return Load(load_type=LoadType.TENSION, magnitude=1000.0, location="LOAD_END")

    @staticmethod
    def _default_boundary(analysis_type: AnalysisType) -> BoundaryCondition:
        if analysis_type == AnalysisType.PRESSURE:
            return BoundaryCondition(bc_type=BoundaryType.PINNED, location="END_A")
        return BoundaryCondition(bc_type=BoundaryType.FIXED, location="FIXED_END")

    # ------------------------------------------------------------------
    # Decision artifact
    # ------------------------------------------------------------------

    def _build_decision_payload(
        self,
        *,
        normalized: dict[str, Any],
        candidates: list[RetrievalCandidate],
        final_path: str,
        selected: RetrievalCandidate | None,
        decision_reason: str,
        case_spec: CaseSpec,
        project_case_library: Path,
    ) -> dict[str, Any]:
        top_candidates = [asdict(c) for c in candidates[:8]]
        return {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "input_summary": {
                "input_type": normalized["input_type"],
                "text_excerpt": normalized["text"][:200] if normalized["text"] else "",
                "step_file": normalized["step_file"],
                "image_file": normalized["image_file"],
                "normalized_geometry_type": normalized["geometry_type"].value,
                "requested_analysis_type": normalized["analysis_type"].value,
                "normalized_analysis_type": normalized["resolved_analysis_type"].value,
                "analysis_adjustment": normalized.get("analysis_adjustment"),
                "normalized_dimensions_mm": {
                    "length": normalized["length"],
                    "width": normalized["width"],
                    "thickness": normalized["thickness"],
                },
            },
            "retrieval_scope": {
                "template_library": "builtin_template_registry",
                "project_case_library": str(Path(project_case_library).resolve()),
            },
            "hit_candidates": top_candidates,
            "final_path": final_path,
            "selected_candidate": asdict(selected) if selected else None,
            "decision_reason": decision_reason,
            "output_case_spec": {
                "case_id": case_spec.metadata.case_id,
                "case_name": case_spec.metadata.case_name,
                "source": case_spec.metadata.source,
                "template_id": case_spec.metadata.template_id,
            },
        }
