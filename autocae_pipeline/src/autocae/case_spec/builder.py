"""CaseSpec builder — 从各种输入格式构建 CaseSpec 对象。

职责：
  - 从 YAML/JSON 文件加载 CaseSpec（YAML 是用户面向的输入格式）
  - 将已验证的 CaseSpec 持久化为 case_spec.json

使用方式：
    builder = CaseSpecBuilder()
    spec = builder.from_yaml("examples/flat_plate_tension.yaml")
    builder.save(spec, run_dir)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from autocae.schemas.case_spec import CaseSpec, CaseSpecMetadata


class CaseSpecBuilder:
    """构建并持久化 CaseSpec 对象。

    这是用户输入进入流水线的第一道关口：
        YAML/JSON 文件 → CaseSpecBuilder.from_yaml() → CaseSpec 对象
                                                          ↓
                                                   CaseSpecValidator
    """

    def from_yaml(self, path: str | Path) -> CaseSpec:
        """从 YAML 文件加载 CaseSpec。

        YAML 是用户最常用的输入格式（见 examples/ 目录）。
        内部使用 yaml.safe_load 解析后交给 Pydantic 校验。

        Args:
            path: YAML 文件路径（如 examples/flat_plate_tension.yaml）

        Returns:
            验证通过的 CaseSpec 对象
        """
        import yaml

        p = Path(path)
        logger.info(f"Loading CaseSpec from YAML: {p}")
        # safe_load 将 YAML 解析为 Python dict，model_validate 触发 Pydantic 校验
        raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8"))
        spec = CaseSpec.model_validate(raw)
        logger.info(f"CaseSpec '{spec.metadata.case_name}' loaded (id={spec.metadata.case_id})")
        return spec

    def from_json(self, path: str | Path) -> CaseSpec:
        """从 JSON 文件加载 CaseSpec。

        流水线内部传递时使用（已由 Builder 生成过的 case_spec.json）。
        """
        p = Path(path)
        logger.info(f"Loading CaseSpec from JSON: {p}")
        spec = CaseSpec.model_validate_json(p.read_text(encoding="utf-8"))
        logger.info(f"CaseSpec '{spec.metadata.case_name}' loaded (id={spec.metadata.case_id})")
        return spec

    def save(self, spec: CaseSpec, output_dir: str | Path) -> Path:
        """将 CaseSpec 序列化为 case_spec.json 写入运行目录。

        这是流水线 Stage 0（验证）完成后的第一个持久化操作，
        保证运行目录中始终有一份规范化的输入记录。

        Args:
            spec:       要保存的 CaseSpec 对象
            output_dir: 运行目录（如 runs/<case_id>/）

        Returns:
            写入的文件路径（runs/<case_id>/case_spec.json）
        """
        out = Path(output_dir) / "case_spec.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(spec.to_json(), encoding="utf-8")
        logger.info(f"CaseSpec saved → {out}")
        return out
