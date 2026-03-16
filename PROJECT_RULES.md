# PROJECT_RULES（V3 对齐版）

## 项目名称
自动化 CAD/CAE

## 0. 适用范围
本规则用于指导所有窗口/成员在本仓库中的设计、开发、评审与重构决策。  
若与历史文档冲突，以 `AutoCAD_CAE_System_Architecture_V3.txt` 与本规则为准。

---

## 1. 项目正式主线（固定）
本项目当前唯一正式执行主线固定为：

`CaseSpec -> CadQuery -> Gmsh -> CalculiX -> Output`

说明：
- `CaseSpec`：统一标准输入对象
- `CadQuery`：几何生成
- `Gmsh`：网格生成
- `CalculiX`：求解输入与求解执行
- `Output`：结果提取、诊断与展示

除非 Master Control 明确决策，不得擅自改写主线顺序、增加并行主线或跳过正式节点。

---

## 2. V3 四条硬约束（不可违背）

1. **检索优先，生成兜底**
- 客户输入（自然语言/图片/STEP 等）进入系统后，必须先检索：
  - `Template Library`
  - `Project Case Library`
- 命中可复用对象时，优先参数化套用。
- 仅在“未命中或置信度不足”时，才允许进入 LLM 生成链路。

2. **LLM 脚本生成 + 自修复是主线能力**
- CAD/Gmsh 等关键阶段允许并鼓励 LLM 直接生成脚本。
- 必须提供受控自动修复（bounded retry）。
- 禁止把 LLM 仅作为“演示功能”或“后置可选插件”。

3. **严禁引入 FEniCSx**
- 不得将 FEniCSx 加入代码、依赖、接口预留、TODO、fallback 路径。
- 一旦出现 FEniCSx 相关实现，必须移除。

4. **交互审查是默认必经流程**
- CAD 与 Gmsh 阶段默认必须经过：
  `自检 -> 预览 -> 用户确认(confirm/edit/abort) -> 下一阶段`
- 不允许默认“无确认直通全链路”。

---

## 3. 开发总原则
1. 先闭环，再增强。
2. 先验证，再扩展。
3. 先稳定接口，再堆功能。
4. 优先可追溯与可复现，而非一次性炫技。
5. 小步增量修改，避免无必要大重构。

---

## 4. 输入与路由规则（新增核心）

### 4.1 输入来源
允许客户输入：
- 自然语言
- 图片（语义抽取）
- STEP/CAD 文件
- 结构化参数

### 4.2 路由顺序（强制）
必须按以下顺序处理：
1. 输入归一化（提取约束与意图）
2. 检索 Template Library / Project Case Library
3. 命中则复用并参数化生成 CaseSpec
4. 未命中才调用 LLM 生成 CaseSpec/阶段脚本

### 4.3 路由决策留痕
每次 intake 必须落盘 `intake_decision.json`，至少包含：
- 输入摘要
- 命中候选与置信度
- 最终路径（reuse / generate）
- 决策原因

---

## 5. 模块职责边界

### 5.1 CaseSpec
职责：
- 统一表达几何、材料、载荷、边界、分析类型、网格偏好、输出请求、模板偏好
- 作为主线唯一标准输入

禁止：
- 不承载求解器 deck 实现细节
- 不承载 CAD/Gmsh/后处理实现细节

### 5.2 CadQuery
职责：
- 几何脚本生成（可由 LLM 生成/修复）
- STEP 导出
- geometry 元数据输出
- CAD 阶段自检与预览数据准备

禁止：
- 不承载求解器配置与执行
- 不承担案例库治理逻辑

### 5.3 Gmsh
职责：
- 网格脚本生成（可由 LLM 生成/修复）
- 网格导出、物理组映射、质量报告
- Mesh 阶段自检与预览数据准备

禁止：
- 不承载 CAD 模板匹配逻辑
- 不承载求解器业务逻辑

### 5.4 CalculiX
职责：
- AnalysisModel 到 CalculiX 输入映射
- 作业执行、日志、状态、失败阶段记录

禁止：
- 不反向定义上游 CaseSpec 结构
- 不将 solver 特有字段污染成全局标准对象

### 5.5 Output
职责：
- 结果摘要、场结果目录、历程数据、诊断、可视化产物组织
- 标准失败归因输出（issue_report）

禁止：
- 不承担求解执行
- 不混入模板提升/案例入库业务

---

## 6. LLM 自修复控制规则（强制）
所有“LLM 生成脚本 -> 执行 -> 修复”流程必须满足：

1. 有界重试：必须定义 `max_attempts`
2. 有限错误域：必须定义 `failure_class_filter`
3. 停止条件：必须定义 `stop_conditions`
4. 全链路审计：每轮记录
   - 输入摘要
   - 脚本版本
   - 运行日志
   - 错误归因
   - 修复动作
   - 轮次结果

禁止：
- 无边界自动重试
- 将失败静默吞掉
- 跨阶段无控制“全链路自动修复”

---

## 7. 交互审查（Review Gate）规则

### 7.1 默认状态机
关键阶段默认状态机：
`auto_check -> preview -> user_confirm(confirm/edit/abort) -> next_stage`

### 7.2 CAD Gate（必需）
- 几何完整性检查
- 拓扑/尺寸合理性检查
- CAD 可视化预览
- 用户确认后才能进 Gmsh

### 7.3 Mesh Gate（必需）
- 网格质量与物理组检查
- Mesh 可视化预览
- 用户确认后才能进 Solver

### 7.4 审查记录
每次 gate 决策必须写入 `review_transcript.json`。

---

## 8. 标准对象与接口文件

### 8.1 模块间解耦载体
优先使用文件接口解耦：
- JSON
- STEP
- 网格文件
- solver 输入文件
- 结果文件

### 8.2 优先稳定对象（变更需说明影响）
- `case_spec.json`
- `analysis_model.json`
- `geometry_meta.json`
- `mesh_groups.json`
- `mesh_quality_report.json`
- `solver_job.json`
- `run_status.json`
- `result_summary.json`
- `field_manifest.json`
- `diagnostics.json`
- `issue_report.json`
- `review_transcript.json`

### 8.3 运行索引
- `runs/index.jsonl` 必须 append-only
- 不允许破坏历史 run 追溯链

---

## 9. 模板与案例沉淀规则

### 9.1 默认落点
- 新运行先进入 `Project Case Library`
- 不因单次案例直接创建 Template

### 9.2 模板孵化流程
`Project Case -> Template Candidate -> 人工审核 -> Template`

### 9.3 必保留字段
- `template_affinity`: `full | partial | none`
- `template_link`: 关联模板版本
- Template 必须带版本号（如 `panel_buckling_v2`）

### 9.4 当前不抢跑
- 不做复杂量化评分系统
- 保留轻量人工审核 + 轻量筛选接口

---

## 10. 库结构规则

### 10.1 Template Library
推荐结构：`Topology -> Family -> Template`

### 10.2 Project Case Library
推荐工程视图结构：`Industry -> Program -> Assembly -> Component -> Case`

### 10.3 Material Library
统一材料属性来源与版本追溯

### 10.4 Feature Library
Feature 用于筛选、标签、参数组合，不作为 Template 主层级骨架

---

## 11. 实施阶段优先级（与 V3 TODO 对齐）

### M1（先做）
1. Intake + 检索优先路由
2. CAD Gate
3. Mesh Gate
4. CAD LLM 脚本生成+自修复
5. Mesh LLM 脚本生成+自修复
6. Review 命令与状态机
7. issue/index 审计产物
8. doctor + tools manifest/integrity
9. run 目录契约冻结

### M2（再做）
1. Project Case Library 落地
2. Candidate/Promote 闭环
3. `template_affinity` / `template_link` 接入
4. 自修复策略统一化
5. retention 与归档

### M3（最后做）
1. 四库治理规范化
2. 模板版本化包与 registry 重构
3. 批量计算与设计研究
4. fixtures + golden-run 回归
5. 协作与发布规范

---

## 12. 接口变更纪律
任何跨模块接口修改必须先说明：
1. 影响模块范围
2. 影响对象与字段
3. 兼容策略
4. 回滚策略

禁止无说明改核心接口。

---

## 13. Codex 执行要求
1. 先声明任务归属模块
2. 先声明输入/输出对象
3. 优先最小必要修改
4. 优先保证主线可继续运行
5. 禁止擅自改主线
6. 禁止引入 FEniCSx
7. 禁止无说明重构核心接口
8. 新对象必须说明位于哪一层、服务哪一段链路
9. 新字段必须说明兼容性影响
10. 输出方案优先提供可直接落地的增量实现

---

## 14. 一句话总纲
本项目是在建设一条：

**检索优先、LLM 主线受控生成、分阶段可审查、结果可追溯、模板可沉淀、面向工程商业化的自动化 CAD/CAE 主线。**
