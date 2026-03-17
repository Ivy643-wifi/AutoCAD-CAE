"""CaseSpec loader — 从各种输入格式构建 CaseSpec 对象。

职责：
  - 从 YAML/JSON 文件加载 CaseSpec（YAML 是用户面向的输入格式）
  - 将已验证的 CaseSpec 持久化为 case_spec.json

使用方式：
    loader = CaseSpecLoader()
    spec = loader.from_yaml("examples/flat_plate_tension.yaml")
    loader.save(spec, run_dir)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from loguru import logger

from autocae.schemas.case_spec import CaseSpec, CaseSpecMetadata


class CaseSpecLoader:
    """构建并持久化 CaseSpec 对象。

    这是用户输入进入流水线的第一道关口：
        YAML/JSON 文件 → CaseSpecLoader.from_yaml() → CaseSpec 对象
                                                          ↓
                                                   CaseSpecValidator
    """

    def from_yaml(self, path: str | Path) -> CaseSpec:
        """从 YAML 文件加载 CaseSpec。"""
        import yaml

        p = Path(path)
        logger.info(f"Loading CaseSpec from YAML: {p}")
        raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8"))
        self._inject_stable_case_id_if_missing(raw=raw, source_path=p)
        spec = CaseSpec.model_validate(raw)
        logger.info(f"CaseSpec '{spec.metadata.case_name}' loaded (id={spec.metadata.case_id})")
        return spec

    def from_json(self, path: str | Path) -> CaseSpec:
        """从 JSON 文件加载 CaseSpec。"""
        p = Path(path)
        logger.info(f"Loading CaseSpec from JSON: {p}")
        raw_text = p.read_text(encoding="utf-8")
        raw: dict[str, Any] = json.loads(raw_text)
        self._inject_stable_case_id_if_missing(raw=raw, source_path=p)
        spec = CaseSpec.model_validate(raw)
        logger.info(f"CaseSpec '{spec.metadata.case_name}' loaded (id={spec.metadata.case_id})")
        return spec

    def save(self, spec: CaseSpec, output_dir: str | Path) -> Path:
        """将 CaseSpec 序列化为 case_spec.json 写入运行目录。"""
        out = Path(output_dir) / "case_spec.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(spec.to_json(), encoding="utf-8")
        logger.info(f"CaseSpec saved → {out}")
        return out

    @staticmethod
    def _inject_stable_case_id_if_missing(*, raw: dict[str, Any], source_path: Path) -> None:
        metadata = raw.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            raw["metadata"] = metadata
        case_id = metadata.get("case_id")
        if isinstance(case_id, str) and case_id.strip():
            return

        digest = hashlib.sha1(str(source_path.resolve()).encode("utf-8")).hexdigest()[:8]
        metadata["case_id"] = f"case_{digest}"


# Backward-compatible alias (was CaseSpecBuilder)
CaseSpecBuilder = CaseSpecLoader
