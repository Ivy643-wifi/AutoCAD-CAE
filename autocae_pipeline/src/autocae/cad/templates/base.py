"""所有 CAD 模板的基类（抽象接口定义）。

设计原则：
    每个具体结构族（GeometryType）对应一个 BaseCADTemplate 子类。
    子类负责用 CadQuery 生成该结构族的参数化几何，导出为 STEP 文件。

CAD 模块只关心几何，不知道网格或求解器的任何细节（G-02 双轨制：
    主轨：CadQuery 生成 → 本模块负责
    备轨：用户直接提供 STEP 文件 → CadBuilder 可直接使用外部 STEP）
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
    """CAD 模板 build() 方法的输出容器。

    包含：
        step_file:      导出的 STEP 文件路径（model.step）
        geometry_meta:  几何元信息（包含包围盒、体积等，传给 MeshBuilder）
        named_faces:    命名面的描述字典（工程名 → 描述，用于调试）
                        注意：这里的值是字符串描述，Gmsh 会用包围盒位置重新映射
    """
    step_file: Path
    geometry_meta: GeometryMeta
    # 命名面：工程含义名称 → 位置描述（如 "FIXED_END": "Face at X = -L/2"）
    # 这些名称会被 MeshBuilder 的位置启发式方法识别并映射到 Physical Group
    named_faces: dict[str, str] = field(default_factory=dict)


class BaseCADTemplate(ABC):
    """所有参数化 CAD 模板的抽象基类。

    每个子类处理一个结构族（GeometryType），实现：
        geometry_type  属性：返回该模板对应的 GeometryType 字符串
        build()        方法：接收 CaseSpec，生成 STEP + GeometryMeta

    如何扩展（添加新结构族）：
        1. 新建 src/autocae/cad/templates/<family>.py
        2. 继承 BaseCADTemplate，实现 geometry_type 和 build()
        3. 在 cad/builder.py 的 _TEMPLATE_REGISTRY 中注册
    """

    @property
    @abstractmethod
    def geometry_type(self) -> str:
        """返回该模板对应的 GeometryType 枚举值（字符串形式）。"""

    @abstractmethod
    def build(self, spec: CaseSpec, output_dir: Path) -> CADResult:
        """构建 CAD 几何并导出 STEP 文件。

        Args:
            spec:       已验证的 CaseSpec（从中读取 geometry 参数和 features）
            output_dir: 运行目录（model.step 和 geometry_meta.json 写入此处）

        Returns:
            CADResult（含 step_file 路径和 GeometryMeta 对象）
        """

    # ------------------------------------------------------------------
    # 共享辅助方法（子类可调用或覆盖）
    # ------------------------------------------------------------------

    def _apply_features(self, workplane: Any, spec: CaseSpec) -> Any:
        """将 CaseSpec 中启用的特征应用到 CadQuery Workplane 上。

        基类提供空实现（不做任何操作）。
        子类可以覆盖此方法实现特定特征（如挖孔、添加加筋等）。
        """
        return workplane

    def _save_step(self, cq_result: Any, output_dir: Path) -> Path:
        """将 CadQuery 几何对象导出为 STEP 文件。

        STEP 是本项目唯一的几何交换格式（设计原则 G-03）。

        Returns:
            写入的 STEP 文件路径（output_dir/model.step）
        """
        step_path = output_dir / "model.step"
        cq_result.val().exportStep(str(step_path))
        return step_path

    def _build_geometry_meta(
        self,
        step_path: Path,
        named_faces: dict[str, list[int]],
        named_edges: dict[str, list[int]],
    ) -> GeometryMeta:
        """构造 GeometryMeta 对象（在 Gmsh 导入并分配实体 tag 之前的初始版本）。

        注意：此时 named_faces/named_edges 的值（Gmsh tag 列表）尚未填充，
              MeshBuilder 在导入 STEP 后会用位置启发式方法重新填充。
        """
        return GeometryMeta(
            step_file=str(step_path),
            source=GeometrySource.CADQUERY,
            named_faces=named_faces,
            named_edges=named_edges,
        )
