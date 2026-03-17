# AutoCAE Pipeline 完整演示指南

## 项目状态总结

✅ **项目能够跑通完整 loop**
AutoCAE Pipeline 是一个功能完整的 8 阶段 CAD/CAE 自动化分析系统（Phase 1 MVP），已实现所有核心模块。

---

## 一、环境准备（关键问题修复）

### 问题诊断
当前虚拟环境配置指向不存在的路径（`E:\Python\Python310\`），需要重建。

### 解决方案

**方案 A：重建虚拟环境（推荐）**

```bash
# 1. 删除旧虚拟环境
rm -rf .venv

# 2. 创建新虚拟环境（使用系统 Python）
python -m venv .venv

# 3. 激活虚拟环境
# Windows (CMD):
.venv\Scripts\activate.bat

# Windows (PowerShell):
.venv\Scripts\Activate.ps1

# Linux/macOS:
source .venv/bin/activate

# 4. 升级 pip（可选但推荐）
pip install --upgrade pip setuptools wheel

# 5. 安装项目依赖
cd autocae_pipeline
pip install -e ".[dev]"
```

**方案 B：使用系统 Python（无 venv）**

```bash
# 确保 Python >= 3.10
python --version

# 直接安装项目
cd autocae_pipeline
pip install -e ".[dev]"
```

---

## 二、完整演示流程（3 部分）

### **PART 1：环境检查（2 分钟）**

```bash
# 检查所有依赖和环境就绪情况
autocae doctor --project-root . --runs-dir runs
```

**期望输出：** 绿色✓ pass 状态，验证以下项：
- ✓ Python 版本 >= 3.10
- ✓ 必要的 pip 包（cadquery, gmsh, pyvista, etc.）
- ✓ CalculiX 可执行文件（如果路径中有 ccx 或 CCX_PATH 设置）
- ✓ runs/ 目录写权限
- ✓ 文件编码（UTF-8）

---

### **PART 2：Dry-Run 演示（5 分钟）**

运行完整 8 阶段流水线，**跳过实际 CalculiX 求解**，演示 CAD 生成、网格划分、评审门等。

```bash
# 从示例 YAML 运行 dry-run
autocae run examples/flat_plate_tension.yaml --dry-run
```

**8 阶段执行流程：**

```
Stage 0: Validate       ✓ 校验 CaseSpec（拓扑、几何、分析类型）
Stage 1: TemplateMatch  ✓ 匹配模板（flat_plate + laminate）
Stage 2: CAD            ✓ 生成 STEP 文件（200×25×2 mm 平板）
Stage 3: CAD Gate       ✓ 自动检查 + 预览 PNG
Stage 4: Mesh           ✓ 生成 Gmsh 网格（mesh.inp）
Stage 5: Mesh Gate      ✓ 自动检查 + 预览 PNG
Stage 6: SolverInput    ✓ 准备 CalculiX 求解卡（solver_job.json）
Stage 7: SolverRun      ✓ 跳过（dry-run 模式）
Stage 8: Postprocess    ✓ 生成诊断信息
```

**期望输出结构：**

```
runs/<case_id>/
├── case_spec.json                 # 输入规范对象
├── model.step                     # CAD 几何（STEP 格式）
├── geometry_meta.json             # 几何元数据
├── mesh.inp                       # Gmsh 生成的有限元网格
├── mesh_groups.json               # 网格节点/单元分组
├── solver_job.json                # CalculiX 求解器输入卡
├── run_status.json                # 运行状态（"COMPLETED_DRY_RUN"）
├── review_transcript.json         # 评审门决策记录
├── geometry_preview_cad.png       # CAD 预览截图
├── geometry_preview_mesh.png      # 网格预览截图
├── diagnostics.json               # 求解诊断信息
└── issue_report.json              # 如有错误的诊断报告
```

---

### **PART 3：验证 & 快速可视化（3 分钟）**

#### 验证运行成功

```bash
# 检查运行状态
cat runs/<case_id>/run_status.json | jq .status

# 期望输出: "COMPLETED_DRY_RUN"
```

#### 查看 CAD 或网格预览

```bash
# 可视化 CAD（仅查看，不交互）
autocae visualize runs/<case_id>/ --mode cad --no-interactive

# 可视化网格（仅查看，不交互）
autocae visualize runs/<case_id>/ --mode mesh --no-interactive

# 完整可视化（CAD + 网格 + 交互）
autocae visualize runs/<case_id>/
```

#### 查看生成的文件

```bash
# 列出所有生成的工件
ls -lh runs/<case_id>/

# 查看 CaseSpec（输入标准对象）
cat runs/<case_id>/case_spec.json | jq . | head -50

# 查看网格统计
cat runs/<case_id>/mesh_groups.json | jq .

# 查看求解器输入卡前几行
head -20 runs/<case_id>/solver_job.json
```

---

## 三、完整工作流演示（进阶，包含实际求解）

### 仅当 CalculiX 已安装时

如果已在 PATH 中或设置 `CCX_PATH` 环境变量，可运行实际求解：

```bash
# 1. 设置 CalculiX 路径（如果不在 PATH 中）
export CCX_PATH=/path/to/ccx

# 2. 运行完整流水线（包含求解）
autocae run examples/flat_plate_tension.yaml

# 期望输出：
# - stage 7 执行 CalculiX → job.frd 生成
# - stage 8 后处理 → 位移、应力、频率提取
# - 最终状态：COMPLETED
```

---

## 四、其他常用命令

### 验证 CaseSpec（不运行）

```bash
autocae validate examples/flat_plate_tension.yaml

# 输出：CaseSpec 有效性验证报告
```

### 查看所有内置模板（Phase 1 共 15 个）

```bash
autocae list-templates

# 输出：
# ┌────────────────────────────────────────────┐
# │ flat_plate          │ laminate, isotropic  │
# │ open_hole_plate     │ laminate (OHT/OHC)   │
# │ cylindrical_shell   │ laminate, isotropic  │
# │ laminated_beam      │ laminate (3-point)   │
# │ stringer_stiffened  │ laminate             │
# │ sandwich_plate      │ laminate, foam core  │
# │ bolted_lap_joint    │ isotropic, contact   │
# └────────────────────────────────────────────┘
```

### 检索优先 Intake（文本输入 → CaseSpec）

```bash
# 从自然语言输入生成 CaseSpec
autocae intake --text "flat plate tension length=200 width=25 thickness=2"

# 输出：
# - runs/<case_id>/case_spec.json
# - runs/<case_id>/intake_decision.json（含置信度、匹配模板）
```

### 从 STEP 文件运行（跳过 CAD 生成）

```bash
# 使用外部 STEP 几何（G-02 双轨模式）
autocae run examples/flat_plate_tension.yaml \
  --step-file /path/to/existing_model.step
```

---

## 五、项目架构要点

### 核心原则（从 PROJECT_RULES.md V3）

| 原则 | 说明 |
|------|------|
| **检索优先** | 模板库优先于 LLM 生成；历史案例复用 |
| **受控 LLM 生成** | 有界重试（max 3 次）+ 自动修复 |
| **交互式评审** | CAD & Mesh 阶段需用户确认（confirm/edit/abort） |
| **完整追溯性** | 所有决策、脚本、错误记录到 JSON |
| **无 FEniCSx** | 仅支持 CalculiX 求解器 |

### 8 阶段管道

```python
Stage 0: Validate       → CaseSpecValidator（业务规则检查）
Stage 1: TemplateMatch  → TemplateRegistry（匹配 + 参数化）
Stage 2: CAD            → CadQuery 脚本生成 or 外部 STEP
Stage 3: CAD Gate       → 自动检查 → 预览 → 用户确认
Stage 4: Mesh           → Gmsh 脚本生成
Stage 5: Mesh Gate      → 自动检查 → 预览 → 用户确认
Stage 6: SolverInput    → CalculiX 求解卡生成
Stage 7: SolverRun      → CCX 执行 or dry-run
Stage 8: Postprocess    → 结果场提取 + 诊断
```

### 关键模块

| 模块 | 文件 | 功能 |
|------|------|------|
| **schemas** | `case_spec.py` | 输入标准对象定义 |
| **templates** | `registry.py` + `cad/*.py` | 15 个内置模板库 |
| **services** | `cad_service.py`, `mesh_service.py` | CAD/网格生成 + LLM 自动修复 |
| **review** | `cad_gate.py`, `mesh_gate.py` | 交互式评审门 |
| **orchestrator** | `pipeline.py` | 8 阶段主控 + 异常恢复 |
| **intake** | `service.py` | 检索优先路由（M1.1） |
| **doctor** | `doctor_service.py` | 环境预检查 |

---

## 六、快速开发命令

### 运行测试

```bash
# 所有测试
pytest

# 单个测试文件
pytest autocae_pipeline/tests/test_pipeline_dry_run.py -v

# 带覆盖率
pytest --cov=autocae_pipeline/src/autocae
```

### 代码质量检查

```bash
# Linting
ruff check autocae_pipeline/src autocae_pipeline/tests

# 类型检查
mypy autocae_pipeline/src/autocae

# 格式化
ruff format autocae_pipeline/src autocae_pipeline/tests
```

### 生成日志（仅限 verbose 模式）

```bash
# 启用详细日志
autocae run examples/flat_plate_tension.yaml --dry-run --verbose
```

---

## 七、演示脚本（自动化）

保存为 `demo.sh`：

```bash
#!/bin/bash
set -e

echo "=========================================="
echo "AutoCAE Pipeline 完整演示"
echo "=========================================="
echo ""

# 步骤 1：环境检查
echo "[1/4] 环境检查..."
autocae doctor --project-root . --runs-dir runs
echo ""

# 步骤 2：运行 dry-run
echo "[2/4] 运行 8 阶段 dry-run（5 分钟）..."
autocae run examples/flat_plate_tension.yaml --dry-run
CASE_ID=$(ls -t runs/ | head -1)
echo "✓ Case ID: $CASE_ID"
echo ""

# 步骤 3：验证输出
echo "[3/4] 验证生成的工件..."
echo "生成的文件列表："
ls -lh runs/$CASE_ID/
echo ""

# 步骤 4：可视化（无交互）
echo "[4/4] CAD 预览..."
autocae visualize runs/$CASE_ID/ --mode cad --no-interactive
echo ""

echo "=========================================="
echo "✓ 演示完成！"
echo "=========================================="
echo "运行目录: runs/$CASE_ID/"
echo "后续命令:"
echo "  - autocae visualize runs/$CASE_ID/         # 交互式可视化"
echo "  - autocae run examples/flat_plate_tension.yaml  # 包含求解"
```

运行：

```bash
chmod +x demo.sh
./demo.sh
```

---

## 八、故障排除

### 问题 1：`autocae: command not found`

```bash
# 检查安装
pip list | grep autocae

# 重新安装
cd autocae_pipeline
pip install -e .
```

### 问题 2：导入错误（`ModuleNotFoundError: No module named 'autocae'`）

```bash
# 确保虚拟环境激活
which python  # 应指向 .venv 中的 python

# 重新安装开发依赖
pip install -e ".[dev]"
```

### 问题 3：CalculiX not found（solver stage 失败）

- **Dry-run 不需要：** `--dry-run` 跳过 stage 7
- **安装 CalculiX：**
  - Windows: 下载 ccx_2.21_env.exe
  - Linux: `apt-get install calculix-ccx`
  - 或设置 `export CCX_PATH=/path/to/ccx`

### 问题 4：网格预览不显示

```bash
# 使用 --no-interactive 生成 PNG 截图
autocae visualize runs/<case_id>/ --mode mesh --no-interactive

# 查看 PNG 文件
ls -la runs/<case_id>/*preview*.png
```

---

## 九、下一步（Phase 2 扩展）

- 更多模板（复合材料、接触、隐式动力学）
- 并行求解器支持
- Web UI（演示、结果浏览）
- 自动优化（参数扫描、敏感性分析）

---

## 快速参考表

| 任务 | 命令 |
|------|------|
| 环境检查 | `autocae doctor` |
| Dry-run | `autocae run <file> --dry-run` |
| 完整求解 | `autocae run <file>` |
| 验证 CaseSpec | `autocae validate <file>` |
| 查看模板 | `autocae list-templates` |
| 可视化 | `autocae visualize <dir>` |
| 文本输入 | `autocae intake --text "..."` |
| 运行测试 | `pytest` |

---

**祝演示愉快！** 🚀
