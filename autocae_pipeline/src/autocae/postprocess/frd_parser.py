"""CalculiX .frd（FRD）结果文件解析器。

.frd 是 CalculiX 输出的主要结果文件格式，是文本-二进制混合格式。
本解析器只处理文本模式（ASCII FRD）。

FRD 文件关键块类型：
    "    2C"        → 节点坐标块（*NODE，含节点 ID 和 x,y,z 坐标）
    " -1 <FIELD>"   → 场量结果块头（含场名、步号）
    " -4"           → 分量定义行（列出该场量的分量名，如 D1,D2,D3）
    " -5"           → 单个分量的说明行
    " -1  <nid>"    → 数据行（节点 ID + 各分量值）
    " -3"           → 块结束标志

本解析器提取：
    - 节点坐标（NodeData）
    - 位移场（DISP：U1, U2, U3）
    - 应力场（STRESS：SXX, SYY, SZZ, SXY, SXZ, SYZ）
    - 反力场（RF：RF1, RF2, RF3）

计算导出量：
    - von Mises 等效应力（由 6 个应力分量计算）
    - 合位移（由 U1, U2, U3 计算）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger


@dataclass
class NodeData:
    """单个节点的坐标数据。"""
    node_id: int
    x: float   # X 坐标 [mm]
    y: float   # Y 坐标 [mm]
    z: float   # Z 坐标 [mm]


@dataclass
class FieldData:
    """一个分析步（增量）中某个场量的全部节点数据。

    field_name: 场量名称（已统一为别名后的标准名，如 'DISP', 'STRESS', 'RF'）
    step:       分析步编号（从 1 开始）
    increment:  增量编号
    components: 分量名列表（如 ['D1', 'D2', 'D3']）
    data:       {节点 ID: [分量值列表]} 字典
    """
    field_name: str
    step: int
    increment: int
    components: list[str]
    data: dict[int, list[float]] = field(default_factory=dict)


@dataclass
class FRDResult:
    """存储从 .frd 文件中提取的全部数据。

    nodes:  {节点 ID: NodeData} 字典（节点坐标表）
    fields: 所有 FieldData 的列表（多个步/场量）
    """
    nodes: dict[int, NodeData] = field(default_factory=dict)
    fields: list[FieldData] = field(default_factory=list)

    def get_field(self, name: str, step: int = -1) -> FieldData | None:
        """按名称（不区分大小写）和步号查找场量。

        step=-1（默认）返回最后一步的结果（通常是最终收敛状态）。
        """
        matches = [f for f in self.fields if f.field_name.upper() == name.upper()]
        if not matches:
            return None
        if step == -1:
            return matches[-1]   # 返回最后一步
        return next((f for f in matches if f.step == step), None)

    def max_displacement_magnitude(self) -> tuple[float, int]:
        """计算所有节点的最大合位移及其节点 ID。

        合位移 = sqrt(U1² + U2² + U3²)

        Returns:
            (max_displacement [mm], node_id)，若无位移场则返回 (0.0, -1)
        """
        u_field = self.get_field("DISP")
        if u_field is None:
            return 0.0, -1
        max_val = 0.0
        max_node = -1
        for nid, vals in u_field.data.items():
            if len(vals) >= 3:
                mag = float(np.sqrt(vals[0]**2 + vals[1]**2 + vals[2]**2))
                if mag > max_val:
                    max_val = mag
                    max_node = nid
        return max_val, max_node

    def max_mises_stress(self) -> tuple[float, int]:
        """计算所有节点的最大 von Mises 等效应力及其节点 ID。

        von Mises 公式：
            σ_mises = sqrt(0.5 * [(σx-σy)² + (σy-σz)² + (σz-σx)² + 6(τxy²+τxz²+τyz²)])

        若场量已包含 MISES 分量则直接读取，否则从 6 个应力分量计算。

        Returns:
            (max_mises [MPa], node_id)，若无应力场则返回 (0.0, -1)
        """
        s_field = self.get_field("STRESS")
        if s_field is None:
            return 0.0, -1

        # 检查是否已有 MISES 分量
        mises_idx = None
        for i, comp in enumerate(s_field.components):
            if "MISES" in comp.upper() or comp.upper() == "V":
                mises_idx = i
                break

        if mises_idx is None and len(s_field.components) >= 6:
            # 从 6 个应力分量（Sxx, Syy, Szz, Sxy, Sxz, Syz）手动计算 von Mises
            max_val = 0.0
            max_node = -1
            for nid, vals in s_field.data.items():
                sx, sy, sz = vals[0], vals[1], vals[2]
                sxy, sxz, syz = vals[3], vals[4], vals[5]
                mises = float(np.sqrt(0.5 * (
                    (sx-sy)**2 + (sy-sz)**2 + (sz-sx)**2
                    + 6*(sxy**2 + sxz**2 + syz**2)
                )))
                if mises > max_val:
                    max_val = mises
                    max_node = nid
            return max_val, max_node

        if mises_idx is not None:
            # 直接读取 MISES 分量
            max_val = 0.0
            max_node = -1
            for nid, vals in s_field.data.items():
                if mises_idx < len(vals):
                    v = abs(vals[mises_idx])
                    if v > max_val:
                        max_val = v
                        max_node = nid
            return max_val, max_node

        return 0.0, -1


class FRDParser:
    """CalculiX .frd 结果文件的文本解析器。

    解析流程：
        1. 读取全部行
        2. 逐行扫描，识别块起始标志
        3. 识别到节点坐标块（"    2C"）→ 调用 _parse_node_block()
        4. 识别到场量结果块（" -1" 或 "100CL"）→ 调用 _parse_result_block()
        5. 返回 FRDResult 对象
    """

    # 场名别名表：将 .frd 文件中的多种写法统一为标准名
    _FIELD_ALIASES: dict[str, str] = {
        "DISPLACEMENTS": "DISP",    # 位移场
        "DISP(CO)":      "DISP",
        "STRESSES":      "STRESS",  # 应力场
        "STRESS(CO)":    "STRESS",
        "FORC":          "RF",      # 反力场
        "FORC(CO)":      "RF",
        "REACTIONS":     "RF",
    }

    def parse(self, frd_path: str | Path) -> FRDResult:
        """解析 .frd 文件，返回结构化结果。

        使用 latin-1 编码（CalculiX 可能输出非 UTF-8 字符）。

        Args:
            frd_path: .frd 文件路径

        Returns:
            FRDResult（含节点坐标和所有场量数据）

        Raises:
            FileNotFoundError: 文件不存在
        """
        path = Path(frd_path)
        if not path.exists():
            raise FileNotFoundError(f"FRD file not found: {path}")

        logger.info(f"FRDParser: parsing {path}")
        result = FRDResult()

        # latin-1 可以处理任意字节，适合解析可能含特殊字符的 CalculiX 输出
        with open(path, encoding="latin-1") as fh:
            lines = fh.readlines()

        i = 0
        while i < len(lines):
            line = lines[i].rstrip("\n")

            # 节点坐标块标志
            if line.startswith("    2C"):
                i = self._parse_node_block(lines, i, result)
                continue

            # 场量结果块标志（多种前缀格式）
            if line.startswith(" -1") or line.startswith("100CL"):
                i = self._parse_result_block(lines, i, result)
                continue

            i += 1

        logger.info(
            f"  Parsed {len(result.nodes)} nodes, "
            f"{len(result.fields)} field blocks"
        )
        return result

    # ------------------------------------------------------------------
    # 私有解析方法
    # ------------------------------------------------------------------

    def _parse_node_block(
        self, lines: list[str], start: int, result: FRDResult
    ) -> int:
        """解析节点坐标块（从 "    2C" 行到 " -3" 结束行）。

        节点数据行格式：
            "    -1     1 1.000E+00 0.000E+00 0.000E+00"
             前缀  节点ID  X坐标      Y坐标      Z坐标
        """
        i = start + 1
        while i < len(lines):
            line = lines[i].rstrip("\n")
            if line.startswith(" -3"):
                return i + 1  # 块结束
            if line.startswith("    -1"):
                parts = line.split()
                if len(parts) >= 5:
                    try:
                        nid = int(parts[1])
                        x, y, z = float(parts[2]), float(parts[3]), float(parts[4])
                        result.nodes[nid] = NodeData(nid, x, y, z)
                    except (ValueError, IndexError):
                        pass
            i += 1
        return i

    def _parse_result_block(
        self, lines: list[str], start: int, result: FRDResult
    ) -> int:
        """解析一个场量结果块（从块头行到 " -3" 结束行）。

        块内结构：
            块头行（" -1 DISPLACEMENTS ..."）→ 提取场名和步号
            " -4"行（分量定义区段开始）
            " -5"行（各分量名称）
            " -1  <nid> v1 v2 ..."（各节点数据行）
            " -3"（块结束）
        """
        header_line = lines[start].rstrip("\n")

        # 从块头提取场量名称并映射到标准别名
        field_name = self._extract_field_name(header_line)
        step_num = self._extract_step_number(header_line)
        field_name = self._FIELD_ALIASES.get(field_name, field_name)

        components: list[str] = []
        data: dict[int, list[float]] = {}

        i = start + 1
        while i < len(lines):
            line = lines[i].rstrip("\n")

            if line.startswith(" -3"):
                # 块结束：若已有分量定义则创建 FieldData 对象
                if components:
                    result.fields.append(FieldData(
                        field_name=field_name,
                        step=step_num,
                        increment=0,
                        components=components,
                        data=data,
                    ))
                return i + 1

            if line.startswith(" -4"):
                # 分量定义区段：接下来的 " -5" 行包含分量名
                # 格式示例：" -4  DISP        4    1"（4 个分量，1 个输出点）
                i += 1
                while i < len(lines) and lines[i].startswith(" -5"):
                    comp_line = lines[i].rstrip("\n")
                    parts = comp_line.split()
                    if len(parts) >= 2:
                        components.append(parts[1])  # 分量名（如 D1, D2, D3）
                    i += 1
                continue

            if line.startswith(" -1"):
                # 节点数据行：节点 ID + 各分量值
                # 格式：" -1     1 1.000E-03 0.000E+00 0.000E+00"
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        nid = int(parts[1])
                        vals = [float(v) for v in parts[2:]]
                        if nid in data:
                            data[nid].extend(vals)  # 同一节点可能有多行数据（高阶场）
                        else:
                            data[nid] = vals
                    except (ValueError, IndexError):
                        pass
            i += 1
        return i

    @staticmethod
    def _extract_field_name(line: str) -> str:
        """从块头行提取场量名称（取第一个非数字的词）。"""
        parts = line.split()
        for p in parts[1:]:
            if p and not p.lstrip("-").replace(".", "").isdigit():
                return p.upper()
        return "UNKNOWN"

    @staticmethod
    def _extract_step_number(line: str) -> int:
        """从块头行提取分析步编号（正则匹配 'STEP=N' 或 'STEP N'）。"""
        m = re.search(r"STEP\s*=?\s*(\d+)", line, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return 1  # 默认步 1
