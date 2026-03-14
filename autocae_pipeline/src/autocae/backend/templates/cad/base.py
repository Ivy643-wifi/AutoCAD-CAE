"""所有 CAD 模板的基类（抽象接口定义）。

设计原则：
    每个具体结构族（GeometryType）对应一个 BaseCADTemplate 子类。
    子类负责用 CadQuery 生成该结构族的参数化几何，导出为 STEP 文件。

CAD 模块只关心几何，不知道网格或求解器的任何细节（G-02 双轨制：
    主轨：CadQuery 生成 → 本模块负责
    备轨：用户直接提供 STEP 文件 → CADService 可直接使用外部 STEP）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autocae.schemas.case_spec import CaseSpec
from autocae.schemas.mesh import GeometryMeta, GeometrySource


@dataclass
class CADResult:
    """CAD 模板 build() 方法的输出容器。"""
    step_file: Path
    geometry_meta: GeometryMeta
    named_faces: dict[str, str] = field(default_factory=dict)


class BaseCADTemplate(ABC):
    """所有参数化 CAD 模板的抽象基类。"""

    @property
    @abstractmethod
    def geometry_type(self) -> str:
        """返回该模板对应的 GeometryType 枚举值（字符串形式）。"""

    @abstractmethod
    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        """构建 CAD 几何并导出 STEP 文件。"""

    def _apply_features(self, workplane: Any, spec: CaseSpec) -> Any:
        """将 CaseSpec 中启用的特征应用到 CadQuery Workplane 上（基类空实现）。"""
        return workplane

    def _save_step(self, cq_result: Any, output_dir: Path) -> Path:
        """将 CadQuery 几何对象导出为 STEP 文件（G-03）。"""
        step_path = output_dir / "model.step"
        cq_result.val().exportStep(str(step_path))
        return step_path

    def _build_geometry_meta(
        self,
        step_path: Path,
        named_faces: dict[str, list[int]],
        named_edges: dict[str, list[int]],
    ) -> GeometryMeta:
        """构造 GeometryMeta 对象（Gmsh 导入前的初始版本）。"""
        return GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces=named_faces,
            named_edges=named_edges,
        )
