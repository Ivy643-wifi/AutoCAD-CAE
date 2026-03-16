# AutoCAE TODO (V3 Priority Plan)

本清单已按 V3 目标重排：检索优先、LLM 主线生成+自修复、非 FEniCSx、分阶段交互审查。

## Status Legend
- `已具备`：仓库已有可用实现
- `部分具备`：有相关能力但未形成 V3 闭环
- `缺失`：尚未实现

---

## M1 - 必须先完成（可用性闭环）

### M1.1 Intake + 检索优先路由
- 优先级：P0
- 当前状态：`缺失`
- 目标：
  1. 新增统一入口 `autocae intake`
  2. 支持输入：自然语言、STEP（图片接口预留）
  3. 先检索 `Template Library / Project Case Library`，再决定是否走 LLM 生成
- 完成标准：
  1. 产出 `intake_decision.json`（命中候选、置信度、是否走生成）
  2. 所有下游都只接收标准 `CaseSpec`

### M1.2 CAD Gate（自检 + 预览 + 用户确认）
- 优先级：P0
- 当前状态：`部分具备`（已有 CAD 可视化能力）
- 目标：
  1. 生成 CAD 后自动几何自检
  2. 输出 CAD 预览图
  3. 用户必须 `confirm/edit/abort`
- 完成标准：
  1. 新增 `autocae preview cad`
  2. 生成 `review_transcript.json`（记录每次用户决策）

### M1.3 Mesh Gate（自检 + 预览 + 用户确认）
- 优先级：P0
- 当前状态：`部分具备`（已有 mesh 质量报告与可视化）
- 目标：
  1. 网格生成后自动质量自检
  2. 输出 mesh 预览图
  3. 用户确认后才允许进入 solver
- 完成标准：
  1. 新增 `autocae preview mesh`
  2. Gate 未通过时禁止进入求解阶段

### M1.4 LLM CAD 脚本生成与受控自修复（主线）
- 优先级：P0
- 当前状态：`缺失`
- 目标：
  1. CAD 阶段支持 LLM 生成脚本
  2. 支持 bounded retry（有上限重试）
  3. 失败输出归因与修复建议
- 完成标准：
  1. 定义 `max_attempts`、`failure_class_filter`、`stop_conditions`
  2. 每轮修复均可审计（脚本、错误、动作、结果）

### M1.5 LLM Gmsh 脚本生成与受控自修复（主线）
- 优先级：P0
- 当前状态：`缺失`
- 目标：与 M1.4 同模式落地到 mesh 阶段
- 完成标准：
  1. 自修复策略与 CAD 阶段一致
  2. mesh 阶段可独立重试，不影响已确认 CAD 产物

### M1.6 交互审查命令面
- 优先级：P0
- 当前状态：`缺失`
- 目标：
  1. 新增 `autocae review`
  2. 提供阶段状态机：`auto_check -> preview -> confirm/edit/abort`
- 完成标准：
  1. CLI 可完整走通 CAD Gate + Mesh Gate
  2. 所有人工决策可追溯

### M1.7 运行审计标准产物
- 优先级：P1
- 当前状态：`缺失`
- 目标：
  1. 每个 run 输出 `issue_report.json`
  2. 建立 `runs/index.jsonl`（append-only）
- 完成标准：
  1. issue 报告含 `error_stage/error_message/root_cause_hint/remediation_hint`
  2. index 可用于 timeline / 检索 / 回放

### M1.8 环境预检与工具完整性
- 优先级：P1
- 当前状态：`缺失`
- 目标：
  1. 新增 `autocae doctor`
  2. 新增 `tools/manifest.yaml` + sha256 校验
- 完成标准：
  1. 检查 Python 依赖、CCX_PATH、可执行可达、写权限、编码
  2. 输出可执行修复建议

### M1.9 Run 目录契约冻结
- 优先级：P1
- 当前状态：`部分具备`（历史 run 结构不完全一致）
- 目标：
  1. 统一 run 目录层级与文件命名
  2. 提供兼容读取层，避免历史数据失效
- 完成标准：
  1. 文档化 run contract v1
  2. `solve/visualize/review` 全部走统一 artifact locator

---

## M2 - 闭环沉淀（run -> case -> template）

### M2.1 Project Case Library 落地
- 优先级：P1
- 当前状态：`缺失`
- 目标：
  1. 建立 Project Case 数据模型
  2. 支持工程视图 + 计算视图
- 完成标准：
  1. run 成功后可落 Project Case
  2. 可从 Project Case 反查对应 run 与产物

### M2.2 Template Candidate + Promote 流程
- 优先级：P1
- 当前状态：`缺失`
- 目标：
  1. 实现 `run review -> approved -> template candidate -> promote`
  2. 新增 `autocae promote --run <case_id>`
- 完成标准：
  1. 有审批元数据与 provenance 记录
  2. 模板提升可回溯至原始 run

### M2.3 template_affinity / template_link 接入
- 优先级：P1
- 当前状态：`缺失`
- 目标：
  1. 在 CaseSpec/Case 元数据中接入两字段
  2. 参与检索与推荐逻辑
- 完成标准：
  1. 检索排序可使用 affinity
  2. 结果页可显示 template_link

### M2.4 Auto-repair 策略统一化
- 优先级：P1
- 当前状态：`缺失`
- 目标：
  1. CAD/mesh 两阶段共享修复策略模型
  2. 标准化错误分类 taxonomy
- 完成标准：
  1. 修复日志结构统一
  2. issue_report 可直接消费修复历史

### M2.5 Retention 与归档
- 优先级：P2
- 当前状态：`缺失`
- 目标：
  1. 定义 run 长期保留与可归档规则
  2. 提供归档命令
- 完成标准：
  1. 不破坏审计链
  2. 可控存储增长

---

## M3 - 治理与规模化

### M3.1 四库治理规范化
- 优先级：P2
- 当前状态：`部分具备`
- 目标：Template / Project Case / Material / Feature 四库统一 schema 与索引
- 完成标准：跨库检索、关联、版本追踪可用

### M3.2 模板版本化包与注册中心重构
- 优先级：P2
- 当前状态：`部分具备`（当前仅基础模板注册）
- 目标：
  1. `template_library/templates/<template_id>/<version>/...`
  2. registry 仅做 matcher/index，payload 独立版本化
- 完成标准：支持分享、回滚、审计、增量发布

### M3.3 批量计算与设计研究
- 优先级：P2
- 当前状态：`缺失`
- 目标：参数扫描、批量运行、结果聚合对比
- 完成标准：可定义 campaign 并自动出汇总报告

### M3.4 回归体系（fixtures + golden runs）
- 优先级：P2
- 当前状态：`部分具备`（已有基础测试）
- 目标：
  1. 拆分 `examples/` 与 `fixtures/`
  2. 建立 golden-run 基线与容差校验
- 完成标准：关键链路回归可在 CI 稳定检测

### M3.5 协作与发布规范
- 优先级：P3
- 当前状态：`缺失`
- 目标：分支策略、评审清单、release checklist、变更日志
- 完成标准：多人协作与发布流程标准化

---

## 建议执行顺序（V3）
1. M1.1
2. M1.2
3. M1.3
4. M1.4
5. M1.5
6. M1.6
7. M1.7
8. M1.8
9. M1.9
10. M2.1
11. M2.2
12. M2.3
13. M2.4
14. M2.5
15. M3.1
16. M3.2
17. M3.3
18. M3.4
19. M3.5
