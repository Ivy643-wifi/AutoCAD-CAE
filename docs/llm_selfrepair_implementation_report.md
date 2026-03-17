# LLM 生成+自修复主线机制 — 实现报告

**基准文档**：AutoCAD_CAE_System_Architecture_V3.txt — 第一节核心结论第2条
**日期**：2026-03-17
**分支**：LLMinterface

---

## 一、V3 要求摘录

> **LLM 生成+自修复为主线机制**
> 各关键阶段允许 LLM 直接生成脚本，并在受控边界内自动修复。
> **不是可选附加功能，而是主执行能力之一。**
>
> 控制边界要求：
> - 必须有界重试：`max_attempts`
> - 必须有限错误域：`failure_class_filter`
> - 必须可追溯：记录每轮脚本、日志、错误归因与修复动作
> - 禁止无边界全链路自动修复

---

## 二、问题诊断（修改前状态）

### 问题 1：`cad_llm_service.py` — 重复函数定义遮蔽了 repair_strategy 导入（Bug）

**文件**：[cad_llm_service.py](../autocae_pipeline/src/autocae/backend/services/cad_llm_service.py)
**位置（修改前）**：第 26-31 行（导入），第 601-643 行（本地重定义）

`cad_llm_service.py` 在文件顶部通过 `from autocae.schemas.repair_strategy import classify_failure, extract_error_message, root_cause_hint, remediation_hint` 导入共用函数，
却在文件末尾（第 601-643 行）重新定义了同名的本地函数。

**Python 行为**：模块级代码从上到下执行，本地 `def classify_failure()` 在加载后覆盖了 import 进来的名称。
因此 `SubprocessScriptExecutor.execute()` 调用的实际是**不完整的本地版本**（缺少 `GEOMETRIC_INVALID` 检测逻辑），而非来自 `repair_strategy` 的完整版本。

**影响**：几何体无效错误（如 `shape is null`）被错误归类为 `runtime_error`，导致修复建议不准确，且破坏了 M2.4 要求的"CAD/Mesh 统一错误分类体系"。

---

### 问题 2：`mesh_llm_service.py` — 未使用共享 repair_strategy（架构不一致）

**文件**：[mesh_llm_service.py](../autocae_pipeline/src/autocae/backend/services/mesh_llm_service.py)
**位置（修改前）**：第 24-41 行（独立的 `MeshLLMRepairConfig`），第 607-650 行（本地函数定义），第 584-604 行（`_write_issue_report` 格式不一致）

具体问题：
1. `MeshLLMRepairConfig` 完全复制了 `RepairConfig` 的字段，没有引用 `repair_strategy.py`，导致双版本配置模型并存
2. 本地定义 `classify_failure`、`extract_error_message`、`root_cause_hint`、`remediation_hint`，不感知 `QUALITY_BELOW_THRESHOLD` 和 `GEOMETRIC_INVALID` 类错误
3. `_write_issue_report` 生成的 JSON 缺少 `repair_history_summary` 字段，与 `cad_llm_issue_report.json` 格式不一致，无法被下游工具统一消费

---

### 问题 3：`pipeline.py` — 模板未命中时缺少自动 LLM 回落（架构缺口）

**文件**：[pipeline.py](../autocae_pipeline/src/autocae/backend/orchestrator/pipeline.py)

V3 架构："命中则优先复用（参数化套用）；**未命中再由 LLM 生成**"

修改前：当 `TemplateRegistry.match()` 返回 `None` 且 `cad_mode == "template"` 时，流水线仍会调用 `CADService.build()`（可能因为 CAD 模板注册表与 TemplateRegistry 解耦而侥幸成功），但后续 Stage 4（`TemplateInstantiator.instantiate(template=None)`)必然失败。
Mesh 阶段同理。

**影响**：用户遇到非标几何类型时（模板库无对应模板），必须手动指定 `--cad-mode llm` 才能继续，违反了 V3"LLM 为主线兜底执行能力"的要求。

---

### 问题 4：`repair_strategy.py` — `classify_failure` 缺少网格质量检测

**文件**：[repair_strategy.py](../autocae_pipeline/src/autocae/schemas/repair_strategy.py)

共用 `classify_failure` 函数可识别 `GEOMETRIC_INVALID`（检测 `geometryexception`、`shape is null`、`invalid geometry`），但没有识别 `QUALITY_BELOW_THRESHOLD` 的逻辑。
当网格脚本主动报告质量不达标时（如打印 `mesh quality fail` 或 `quality_below_threshold`），错误会被归入 `RUNTIME_ERROR`，修复建议不准确。

---

## 三、修改内容

### 修改 1：`cad_llm_service.py` — 删除遮蔽导入的本地函数定义

**文件**：[cad_llm_service.py:601-643](../autocae_pipeline/src/autocae/backend/services/cad_llm_service.py)

**操作**：删除文件末尾重复定义的 `classify_failure`、`extract_error_message`、`root_cause_hint`、`remediation_hint`，替换为一行注释说明来源。

**效果**：
- `SubprocessScriptExecutor.execute()` 现在使用来自 `repair_strategy` 的完整 `classify_failure`（包含 `GEOMETRIC_INVALID` 检测）
- `_write_issue_report` 继续使用已正确引用的 `build_issue_report`
- 消除约 43 行重复代码

```diff
- def classify_failure(log_text: str) -> str:
-     text = log_text.lower()
-     if "syntaxerror" in text:
-         return "syntax_error"
-     ...（旧版，不完整）
-
- def extract_error_message(...) -> str: ...
- def root_cause_hint(...) -> str: ...
- def remediation_hint(...) -> str: ...
+ # M2.4: classify_failure, extract_error_message, root_cause_hint, remediation_hint
+ # 由 autocae.schemas.repair_strategy 统一提供，已在文件顶部导入，此处不重复定义。
```

---

### 修改 2：`mesh_llm_service.py` — 接入共享 repair_strategy

**文件**：[mesh_llm_service.py](../autocae_pipeline/src/autocae/backend/services/mesh_llm_service.py)

**操作（3处）**：

**2a）** 导入 `repair_strategy` 模块，并将 `MeshLLMRepairConfig` 设为 `RepairConfig` 的类型别名（保持向后兼容）：

```diff
+ from autocae.schemas.repair_strategy import (
+     RepairConfig,
+     build_issue_report,
+     classify_failure,
+     extract_error_message,
+ )
+
- @dataclass
- class MeshLLMRepairConfig:
-     """Bounded retry controls required by V3."""
-     max_attempts: int = 3
-     ...（独立定义，与 RepairConfig 重复）
+ # M2.4: MeshLLMRepairConfig 是 RepairConfig 的类型别名，保持向后兼容
+ MeshLLMRepairConfig = RepairConfig
```

**2b）** 更新 `_write_issue_report` 使用统一的 `build_issue_report`（增加 `repair_history_summary`）：

```diff
- last = attempts[-1] if attempts else {}
- err_class = str(last.get("error_class", "runtime_error"))
- err_msg = str(last.get("error_message", "unknown error"))
- report = {
-     "error_stage": "mesh_llm",
-     "error_class": err_class,
-     "error_message": err_msg,
-     "root_cause_hint": root_cause_hint(err_class),
-     "remediation_hint": remediation_hint(err_class, stop_reason),
-     "stop_reason": stop_reason,
- }
+ # M2.4: 使用共享 build_issue_report，结构统一
+ report = build_issue_report(
+     stage="mesh_llm",
+     stop_reason=stop_reason,
+     attempts=attempts,
+ )
```

**2c）** 删除本地重复函数定义（约 43 行）：

```diff
- def classify_failure(log_text: str) -> str: ...
- def extract_error_message(log_text: str) -> str: ...
- def root_cause_hint(error_class: str) -> str: ...
- def remediation_hint(error_class: str, stop_reason: str) -> str: ...
+ # M2.4: classify_failure, extract_error_message 由 autocae.schemas.repair_strategy 统一提供，
+ # 已在文件顶部导入，此处不重复定义。
```

---

### 修改 3：`pipeline.py` — V3 检索优先、LLM兜底自动回落

**文件**：[pipeline.py:159-230](../autocae_pipeline/src/autocae/backend/orchestrator/pipeline.py)

**操作**：在 Stage 1（模板匹配）之后、Stage 2（CAD）之前，当 `template is None` 时自动将 `effective_cad_mode` 和 `effective_mesh_mode` 切换为 `"llm"`，并记录 warning 日志。

```python
# V3 检索优先、LLM兜底：模板未命中时自动切换到 LLM 模式
effective_cad_mode = self.cad_mode
effective_mesh_mode = self.mesh_mode
if template is None and not step_file:
    if self.cad_mode == "template":
        logger.warning(
            "[Pipeline] No template matched — auto-switching CAD to LLM mode (V3: 未命中再由LLM生成)"
        )
        effective_cad_mode = "llm"
    if self.mesh_mode == "template":
        logger.warning(
            "[Pipeline] No template matched — auto-switching Mesh to LLM mode (V3: 未命中再由LLM生成)"
        )
        effective_mesh_mode = "llm"
```

Stage 2 和 Stage 3 的模式判断从 `self.cad_mode` / `self.mesh_mode` 改为 `effective_cad_mode` / `effective_mesh_mode`。

**效果**：
- 用户仍可通过 `--cad-mode llm` 显式指定
- 模板命中时行为不变（`effective_cad_mode = self.cad_mode`）
- 模板未命中且有外部 STEP 文件时（`step_file is not None`）不触发自动回落（STEP 路径优先级更高）

---

### 修改 4：`repair_strategy.py` — 增强 classify_failure 检测

**文件**：[repair_strategy.py:167-185](../autocae_pipeline/src/autocae/schemas/repair_strategy.py)

**操作**：在 `classify_failure` 中增加网格质量不达标的检测逻辑：

```diff
+ # 网格质量不达标：来自 mesh_quality_report.json 写入失败或脚本主动报告质量问题
+ if (
+     "quality below" in text
+     or "quality_below_threshold" in text
+     or "mesh quality" in text and "fail" in text
+     or "skewness" in text and "exceed" in text
+     or "aspect ratio" in text and "exceed" in text
+ ):
+     return ErrorClass.QUALITY_BELOW_THRESHOLD
  return ErrorClass.RUNTIME_ERROR
```

**检测的关键词**：
| 关键词模式 | 适用场景 |
|---|---|
| `quality below` | 脚本内显式打印质量不达标 |
| `quality_below_threshold` | 来自 `MeshQualityReport` 的字符串化错误 |
| `mesh quality` + `fail` | Gmsh 脚本的质量检查报错 |
| `skewness` + `exceed` | 高偏斜度网格告警 |
| `aspect ratio` + `exceed` | 高长宽比网格告警 |

---

## 四、修改文件汇总

| 文件 | 修改类型 | 修改概要 |
|------|----------|----------|
| [cad_llm_service.py](../autocae_pipeline/src/autocae/backend/services/cad_llm_service.py) | Bug修复 | 删除遮蔽 repair_strategy 导入的本地函数重定义（~43行） |
| [mesh_llm_service.py](../autocae_pipeline/src/autocae/backend/services/mesh_llm_service.py) | 架构统一 | 引入 repair_strategy 共享组件，统一 MeshLLMRepairConfig、issue_report 格式（~50行变更） |
| [pipeline.py](../autocae_pipeline/src/autocae/backend/orchestrator/pipeline.py) | 功能实现 | 模板未命中时自动切换 LLM 模式（V3 "未命中再由LLM生成"），新增约15行逻辑 |
| [repair_strategy.py](../autocae_pipeline/src/autocae/schemas/repair_strategy.py) | 功能增强 | classify_failure 增加 QUALITY_BELOW_THRESHOLD 检测（+8行） |

---

## 五、验证结果

```
repair_strategy imports OK
MeshLLMRepairConfig is RepairConfig: True
CadLLMRepairConfig is RepairConfig: True
syntax_error: ErrorClass.SYNTAX_ERROR
quality_below_threshold: ErrorClass.QUALITY_BELOW_THRESHOLD
geometric_invalid: ErrorClass.GEOMETRIC_INVALID
All OK
```

---

## 六、V3 要求对照

| V3 要求 | 实现状态 |
|---------|----------|
| 必须有界重试（max_attempts） | ✅ 已有，`RepairConfig.max_attempts`，CAD/Mesh 共用 |
| 必须有限错误域（failure_class_filter） | ✅ 已有，`RepairConfig.failure_class_filter`，CAD/Mesh 共用 |
| 必须可追溯（记录每轮脚本、日志、错误归因） | ✅ 已有，`cad_llm_repair_audit.json` / `mesh_llm_repair_audit.json` + `issue_report` |
| 禁止无边界全链路自动修复 | ✅ `stop_conditions` 中的 `max_attempts_reached`、`failure_class_not_allowed`、`repeated_failure_limit` 均已实现 |
| 未命中模板再由 LLM 生成（检索优先、LLM兜底） | ✅ **本次新增**，`pipeline.py` 中模板未命中时自动切换 LLM 模式 |
| 报告格式统一（CAD/Mesh issue_report 可直接消费） | ✅ **本次修复**，`mesh_llm_service._write_issue_report` 现在使用 `build_issue_report` |
| 错误分类体系统一 | ✅ **本次修复**，CAD/Mesh 均使用 `repair_strategy.classify_failure`，删除重复本地定义 |

---

*报告生成于 2026-03-17，针对 AutoCAD_CAE_System_Architecture_V3.txt 核心结论第2条"LLM 生成+自修复为主线机制"实施。*
