# 📐 AutoCAE Pipeline 模块依赖与信息流关系图

## 模块依赖树（从下往上）

```
┌─────────────────────────────────────────────────────────────────────┐
│                          CLI 入口层                                  │
│                        (cli.py 8条命令)                             │
└──────┬─────────────────────────────────────────────────────────────┘
       │
       ├─→ [intake] ──→ IntakeService
       │                    │
       │                    ├─→ TemplateRegistry (模板匹配)
       │                    └─→ 历史案例库查询
       │
       ├─→ [run] ──────→ PipelineRunner
       │                    │
       │                    ├─→ Stage 0: CaseSpecValidator
       │                    │            │
       │                    │            └─→ validator.py (Layer A)
       │                    │
       │                    ├─→ Stage 1: TemplateRegistry
       │                    │
       │                    ├─→ Stage 2: CADService
       │                    │            │
       │                    │            ├─→ FlatPlateTemplate
       │                    │            ├─→ OpenHolePlateTemplate
       │                    │            ├─→ CylindricalShellTemplate
       │                    │            ├─→ LaminatedBeamTemplate
       │                    │            ├─→ StringerStiffenedPanelTemplate
       │                    │            ├─→ SandwichPlateTemplate
       │                    │            └─→ BoltedLapJointTemplate
       │                    │
       │                    ├─→ Stage 3: CadGateService
       │                    │            │
       │                    │            ├─→ VisualizationService (预览)
       │                    │            └─→ DiagnosticsValidator (auto_check)
       │                    │
       │                    ├─→ Stage 4: MeshService
       │                    │
       │                    ├─→ Stage 5: MeshGateService
       │                    │            │
       │                    │            ├─→ VisualizationService
       │                    │            └─→ DiagnosticsValidator
       │                    │
       │                    ├─→ Stage 6: CalculiXAdapter
       │                    │            │
       │                    │            └─→ TemplateInstantiator
       │                    │                    │
       │                    │                    └─→ AnalysisModel schema
       │                    │
       │                    ├─→ Stage 7: SolverRunner (CCX execution)
       │                    │
       │                    ├─→ Stage 8: PostprocessEngine
       │                    │
       │                    └─→ ArtifactLocator (运行目录兼容)
       │
       ├─→ [review] ──→ CadGateService + MeshGateService
       │
       ├─→ [preview] ──→ CadGateService / MeshGateService
       │
       ├─→ [solve] ──→ SolverRunner + PostprocessEngine
       │
       ├─→ [visualize] ──→ VisualizationService
       │
       ├─→ [validate] ──→ CaseSpecValidator
       │
       ├─→ [list-templates] ──→ TemplateRegistry
       │
       └─→ [doctor] ──→ DoctorService

┌─────────────────────────────────────────────────────────────────────┐
│                     数据模型层 (Pydantic Schemas)                    │
│                                                                       │
│  ├─ case_spec.py          CaseSpec标准对象（输入）                  │
│  ├─ analysis_model.py     AnalysisModel参数化模型                   │
│  ├─ mesh.py               GeometryMeta, MeshGroups等（中间）        │
│  ├─ solver.py             RunStatus, SolverJob等（中间）            │
│  └─ postprocess.py        ResultSummary, FieldManifest（输出）      │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 📊 模块功能矩阵

| 模块 | 职责 | 输入 | 输出 | 依赖 |
|------|------|------|------|------|
| **loader.py** | YAML/JSON → CaseSpec | 文件路径 | CaseSpec | Pydantic |
| **validator.py** | Layer A验证 | CaseSpec | 验证结果 | case_spec.py |
| **intake/service.py** | 检索优先路由 | 文本/STEP/图片 | CaseSpec + intake_decision | TemplateRegistry |
| **template/registry.py** | 模板库管理与匹配 | CaseSpec | CaseTemplate or None | case_spec.py |
| **template/cad/*.py** | CAD模板（7种） | CaseSpec | model.step + geometry_meta | CadQuery |
| **cad_service.py** | CAD生成调度 | CaseSpec | CADResult | CAD模板 |
| **cad_llm_service.py** | LLM CAD生成+修复 | CaseSpec | model.step + metadata | Claude API |
| **cad_gate.py** | CAD评审门 | model.step | decision + transcript | VisualizationService |
| **mesh_service.py** | Gmsh网格生成 | model.step | mesh.inp + mesh_groups | Gmsh |
| **mesh_llm_service.py** | LLM Mesh生成+修复 | model.step | mesh.inp | Claude API |
| **mesh_gate.py** | Mesh评审门 | mesh.inp | decision + transcript | VisualizationService |
| **template/instantiator.py** | CaseSpec → AnalysisModel | CaseSpec + 模板 | AnalysisModel | analysis_model.py |
| **solver_service.py** | CalculiX适配与求解 | AnalysisModel + mesh.inp | job.frd + run_status | CalculiX |
| **postprocess_service.py** | 结果提取 | job.frd | ResultSummary + FieldManifest | scipy |
| **visualization_service.py** | 可视化 | model.step/mesh.inp/job.frd | PNG预览 | PyVista |
| **orchestrator/pipeline.py** | 8阶段主控 | case_file | PipelineResult | 所有服务 |
| **orchestrator/artifact_locator.py** | 运行目录兼容 | run_dir | 统一的文件访问接口 | pathlib |
| **doctor_service.py** | 环境检查 | project_root | EnvironmentReport | subprocess |

---

## 🔄 信息流动（数据沿着哪条路）

### 📥 输入流（Intake）
```
用户输入 (text/STEP/image)
    ↓ IntakeService
关键字匹配 ──→ _parse_geometry_from_text() ──→ GeometryType候选
    ↓
模板库查询 ──→ TemplateRegistry.match() ──→ CaseTemplate候选
    ↓
历史案例查询 ──→ project_case_library/*.json ──→ 历史CaseSpec候选
    ↓
置信度评分 ──→ 比较 ≥75%?
              ├─Yes → CaseSpec复用 ✓
              └─No  → LLM生成（待实现）→ CaseSpec ✓
    ↓
输出：case_spec.json + intake_decision.json
```

### 🏭 生产流（Pipeline）
```
case_spec.json
    ↓ [S0] Validate
CaseSpec ✓
    ↓ [S1] TemplateMatch
CaseTemplate or LLMGeneration
    ↓ [S2] CAD
model.step + geometry_meta.json
    ↓ [S3] CadGate ★
用户confirm ✓
    ↓ [S4] Mesh
mesh.inp + mesh_groups.json
    ↓ [S5] MeshGate ★
用户confirm ✓
    ↓ [S6] SolverInput
AnalysisModel + solver_job.json + job.inp
    ↓ [S7] SolverRun
job.frd + run_status.json
    ↓ [S8] Postprocess
ResultSummary + FieldManifest + Diagnostics + *.png
    ↓
输出：runs/<case_id>/*（完整结果集）
```

### 🔀 评审流（Gate Confirm）
```
model.step / mesh.inp
    ↓
CadGateService / MeshGateService
    ├─→ auto_check（检查文件存在、格式、与CaseSpec一致）
    ├─→ preview（生成PNG截图）
    └─→ user_confirm（决策：confirm/edit/abort）
         │
         ├─confirm → next_stage_allowed=True → review_transcript ✓
         ├─edit    → 设计者手动编辑源文件 → 重新运行此stage
         └─abort   → pipeline停止
```

---

## 🎯 各模块之间的调用关系

```
┌──────────────────────────────────────────────────────────┐
│                 PipelineRunner (主控)                    │
└──────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
              ↓               ↓               ↓
        CaseSpecLoader  CaseSpecValidator  TemplateRegistry
              │               ↑               ↑
              ├───────────────┴───────────────┤
              │
              ↓
         CaseSpec (schema)
              │
              ├─────────────────────────────────────────┐
              │                                         │
              ↓                                         ↓
         CADService ───→ CadGateService ─→ VisualizationService
         (或 CadLLMBuildService)          |
              ↓                           ├─→ DiagnosticsValidator
         model.step              ┌────────┘
              │                  │
              ├─────────────────────→ MeshService
              │                  (或 MeshLLMBuildService)
              │                        ↓
              │                   mesh.inp ───→ MeshGateService
              │                                       │
              │                        ┌──────────────┘
              │                        │
              ├────────────────────────┴──→ TemplateInstantiator
              │                                     ↓
              │                            AnalysisModel
              │                                     │
              ├─────────────────────────────────────┤
              │                                     │
              ↓                                     ↓
         CalculiXAdapter ←─────────────────────────┘
              │
              ├─→ solver_job.json
              ├─→ job.inp
              │
              ↓
         SolverRunner (CalculiX execution)
              │
              ├─→ job.frd
              ├─→ run_status.json
              │
              ↓
         PostprocessEngine
              │
              ├─→ ResultSummary
              ├─→ FieldManifest
              ├─→ Diagnostics
              │
              ↓
         PipelineResult (返回给CLI)
              │
              ├─→ success / error_message
              └─→ 结果汇总表（max_displacement, max_stress, buckling_factor）
```

---

## 🔌 关键接口与协议

### 1. CaseSpec（最核心的数据对象）

```python
CaseSpec {
    metadata: CaseSpecMetadata
        ├─ case_id: str
        ├─ case_name: str
        ├─ created_at: datetime
        └─ version: str

    geometry: Geometry
        ├─ geometry_type: GeometryType (enum)
        ├─ topology: Topology (enum)
        └─ dimensions: dict[str, float]  # length, width, thickness等

    material: Material
        ├─ name: str
        ├─ matrix_type: str (e.g., "epoxy")
        ├─ fiber_type: str
        └─ properties: dict (E, nu, rho, Xt, Xc...)

    layup: list[LayupLayer]
        └─ angle: float (0, 45, 90, -45 degree)

    analysis_type: AnalysisType (enum)
        # static_tension, buckling, modal, etc.

    loads: list[Load]
        ├─ load_type: LoadType
        ├─ magnitude: float
        └─ region: str

    boundary_conditions: list[BoundaryCondition]
        ├─ boundary_type: BoundaryType
        ├─ region: str
        └─ axis: str (X/Y/Z)

    features: list[Feature]
        ├─ name: FeatureName (enum)
        └─ enabled: bool

    mesh_preferences: MeshPreferences
        ├─ element_size: float
        ├─ boundary_layer: bool
        └─ refinement_regions: list[str]
}
```

### 2. CADResult（CAD阶段输出）

```python
CADResult {
    step_file: Path          # model.step 文件位置
    geometry_meta: GeometryMeta  # 几何元数据
        ├─ bounding_box: dict (xmin, xmax, ymin, ymax, zmin, zmax)
        ├─ volume: float
        ├─ surface_area: float
        └─ source: GeometrySource (CadQuery or External)
}
```

### 3. MeshResult（Mesh阶段输出）

```python
MeshResult {
    mesh_inp_file: Path          # mesh.inp
    mesh_groups: MeshGroups      # 网格划分信息
        ├─ element_groups: dict[str, ElementGroup]
        │   └─ elements: list[int]
        └─ node_groups: dict[str, NodeGroup]
            └─ nodes: list[int]

    mesh_quality: MeshQualityReport  # 网格质量指标
        ├─ total_elements: int
        ├─ total_nodes: int
        ├─ aspect_ratio_mean: float
        ├─ aspect_ratio_max: float
        └─ skewness_max: float
}
```

### 4. SolverResult（求解后输出）

```python
SolverResult {
    frd_file: Path           # job.frd 结果文件
    run_status: RunStatus    # COMPLETED / FAILED
    exit_code: int           # 0 = success
}
```

### 5. ResultSummary（最终结果汇总）

```python
ResultSummary {
    max_displacement: float | None      # mm
    max_mises_stress: float | None      # MPa
    buckling_load_factor: float | None  # —
    natural_frequencies: list[float] | None  # Hz (for modal)
    temperature_range: tuple[float, float] | None  # K (for thermal)
}
```

---

## 🔑 关键枚举类型

```python
# 拓扑（大类）
Topology = {LAMINATE, SHELL, BEAM, PANEL, SANDWICH, JOINT}

# 几何类型（细分）
GeometryType = {
    FLAT_PLATE, OPEN_HOLE_PLATE, NOTCHED_PLATE,
    CYLINDRICAL_SHELL, PRESSURE_SHELL,
    LAMINATED_BEAM,
    STRINGER_STIFFENED_PANEL,
    SANDWICH_PLATE,
    BOLTED_LAP_JOINT
}

# 分析类型
AnalysisType = {
    STATIC_TENSION, STATIC_COMPRESSION, BENDING, BUCKLING,
    MODAL, IMPACT, FATIGUE, THERMAL, SHEAR, TORSION, PRESSURE
}

# 载荷类型
LoadType = {
    TENSION, COMPRESSION, BENDING, SHEAR, TORSION,
    PRESSURE, POINT_FORCE, MOMENT, THERMAL
}

# 边界条件类型
BoundaryType = {
    FIXED, PINNED, SIMPLY_SUPPORTED, SYMMETRY, ...
}

# 运行状态
RunStatusEnum = {PENDING, RUNNING, COMPLETED, FAILED}

# Gate决策
GateDecision = {confirm, edit, abort}
```

---

## 🏛️ 架构设计原则

1. **分层设计** — 数据模型层 → 服务层 → 流水线层 → CLI层
2. **单一职责** — 每个服务只负责一个Stage或功能
3. **依赖倒置** — 服务间通过schema（Pydantic模型）而非紧耦合
4. **文件接口** — 各Stage通过`runs/<case_id>/`目录下的JSON/INP/FRD文件交互
5. **检索优先** — 模板库 > 历史案例 > LLM生成（递阶降级）
6. **可追溯性** — 所有决策和工件记录在review_transcript.json和runs/index.jsonl
7. **交互式门** — 不能自动跳过CAD/Mesh确认，保留人工审核
8. **有界重试** — LLM生成和修复都有max_attempts和stop_conditions

---

## 💾 文件系统契约

```
runs/
├── index.jsonl                           # 全局timeline（每行一个完整JSON）
└── <case_id>/                            # 单个案例目录
    ├── case_spec.json                    # 输入（必有）
    ├── intake_decision.json              # 入口决策
    ├── model.step                        # CAD输出（必有）
    ├── geometry_meta.json                # 几何元数据
    ├── mesh.inp                          # Gmsh输出（必有）
    ├── mesh_groups.json                  # 网格划分
    ├── solver_job.json                   # CalculiX任务
    ├── job.inp                           # CalculiX输入卡片
    ├── job.frd                           # CalculiX结果
    ├── run_status.json                   # 流水线状态
    ├── review_transcript.json            # 评审决策记录
    ├── issue_report.json                 # 错误诊断
    ├── field_manifest.json               # 可用输出字段
    ├── diagnostics.json                  # 求解诊断
    ├── cad_preview.png                   # CAD预览
    ├── mesh_preview.png                  # 网格预览
    └── results_preview.png               # 结果预览
```

这个结构既支持**现代化的扁平设计**（推荐），也兼容**旧式的分阶段子目录结构**。
