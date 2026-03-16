# TODO2 - Old -> Current 最小移植清单（按文件级别）

目标：从 `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2` 只移植对 V3 必要且低风险的能力。  
约束：**不迁移任何 FEniCSx 相关代码**。

## 0. 迁移原则
1. 只移植“能力模式”，不整块复制旧架构。
2. 现仓 `autocae_pipeline` 作为唯一主干。
3. 每项必须有明确目标文件、最小动作、验收标准。

---

## M1（立即执行，最小闭环）

### 1) LLM Intake 兜底（检索未命中时）
- 源文件：
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\backend\agents\spec_agent_v2.py`
- 目标文件（新增）：
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\intake\llm_spec_provider.py`
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\tests\test_intake_llm_provider.py`
- 最小动作：
  - [ ] 移植 `create_specs_from_prompt/revise_specs_with_feedback` 的交互模式
  - [ ] 输出从 “geometry_spec/mesh_spec/analysis_spec dict” 改为严格 `CaseSpec`
  - [ ] 接入现有 `intake/service.py` 的未命中分支
- 验收标准：
  - [ ] 检索未命中时可生成合法 `case_spec.json`
  - [ ] 生成失败有可读错误且不破坏现有 intake 路由

### 2) 交互式 review 会话命令（单入口）
- 源文件：
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\autosim_v2_cli.py`（`_spec_confirmation_loop/_geometry_stage_loop/_mesh_stage_loop`）
- 目标文件（新增/修改）：
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\review\session.py`（新增）
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\cli.py`（新增 `autocae review` 命令）
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\tests\test_review_session.py`（新增）
- 最小动作：
  - [ ] 将旧的分阶段确认状态机移植为 `confirm/edit/abort`
  - [ ] 支持从 mesh 回退至 cad（最小回退能力）
  - [ ] 将所有决策写入 `review_transcript.json`
- 验收标准：
  - [ ] `autocae review <run_dir>` 可完成 CAD->Mesh 双 gate 流程
  - [ ] 未 confirm 时禁止进入 solver

### 3) CAD LLM 提示增强（few-shot 示例注入）
- 源文件：
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\backend\agents\cad_agent.py`（`_load_example_snippets` 与 prompt 构造）
- 目标文件（修改）：
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\services\cad_llm_service.py`
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\tests\test_cad_llm_service.py`
- 最小动作：
  - [ ] 增加 examples 目录可选输入（few-shot）
  - [ ] 统一 “初次生成/修复” 提示模板
- 验收标准：
  - [ ] 在同等输入下脚本生成稳定性提升（失败率下降）

### 4) Mesh LLM 提示增强（few-shot 示例注入）
- 源文件：
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\backend\agents\mesh_agent.py`（`_load_example_snippets` 与 prompt 构造）
- 目标文件（修改）：
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\services\mesh_llm_service.py`
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\tests\test_mesh_llm_service.py`
- 最小动作：
  - [ ] 引入 examples few-shot
  - [ ] 增强修复回合错误上下文
- 验收标准：
  - [ ] 网格脚本修复成功率可观测提升

### 5) 日志去噪与修复上下文裁剪
- 源文件：
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\utils\log_utils.py`
- 目标文件（新增/修改）：
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\utils\log_utils.py`（新增）
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\services\cad_llm_service.py`
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\services\mesh_llm_service.py`
- 最小动作：
  - [ ] 移植 `clean_noisy_output`
  - [ ] 在修复 prompt 前清洗/截断日志
- 验收标准：
  - [ ] 修复 prompt 更短且信息密度更高
  - [ ] 不丢失关键错误行

---

## M2（稳定性补强）

### 6) 统一脚本执行环境 runner
- 源文件：
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\backend\core\env_manager.py`
- 目标文件（新增/修改）：
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\runtime\env_runner.py`（新增）
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\services\cad_llm_service.py`
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\services\mesh_llm_service.py`
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\tests\test_env_runner.py`（新增）
- 最小动作：
  - [ ] 迁移“可配置 Python/Conda 运行器”模式
  - [ ] 去掉 `fenicsx_env` 语义，改为通用执行环境配置
- 验收标准：
  - [ ] 本地 `.venv` 与可选 Conda 都可驱动 LLM 脚本执行

### 7) preview 渲染能力兜底
- 源文件：
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\backend\services\geometry_render_service.py`
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\backend\services\mesh_render_service.py`
- 目标文件（修改）：
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\review\cad_gate.py`
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\review\mesh_gate.py`
- 最小动作：
  - [ ] 移植“预览失败时自动降级/重试”思路
  - [ ] 统一预览产物命名与输出路径
- 验收标准：
  - [ ] gate 预览失败可给出可执行修复提示，不中断全局流程

### 8) examples 目录自动匹配策略
- 源文件：
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\autosim_v2_cli.py`（`_resolve_examples_dir`）
- 目标文件（新增）：
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\src\autocae\backend\llm\examples_resolver.py`
- 最小动作：
  - [ ] 根据 geometry_type/关键词自动选择 few-shot 示例目录
- 验收标准：
  - [ ] 无需手工指定 examples 也能走默认最佳目录

---

## M3（治理与清理）

### 9) 历史脚本资产结构化归档
- 源文件参考：
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\Geometry\scripts\*`
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\Meshes\scripts\*`
- 目标目录（新增）：
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\fixtures\llm_scripts\cad\`
  - `D:\VibeCoding\AuToCAD-CAE\autocae_pipeline\fixtures\llm_scripts\mesh\`
- 最小动作：
  - [ ] 选取少量高质量脚本作为回归 fixtures（不全量搬运）
- 验收标准：
  - [ ] fixtures 可用于 golden-run 或 prompt regression

### 10) 迁移后接口文档更新
- 目标文件（修改）：
  - `D:\VibeCoding\AuToCAD-CAE\AutoCAD_CAE_System_Architecture_V3.txt`
  - `D:\VibeCoding\AuToCAD-CAE\PROJECT_RULES.md`
  - `D:\VibeCoding\AuToCAD-CAE\TODO.md`
- 最小动作：
  - [ ] 补充“已迁移能力/未迁移能力”清单
  - [ ] 标注各命令与对象契约版本
- 验收标准：
  - [ ] 文档与代码状态一致，不出现过期设计描述

---

## 禁止迁移（明确排除）
- 以下 old 文件只可参考，不可迁入主线：
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\backend\agents\fenicsx_agent.py`
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\backend\services\fenicsx_service.py`
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\examples\*\solve_fenicsx_example.py`
  - `D:\VibeCoding\AuToCAD-CAE-OLD\autosim_v2\backend\orchestrator\pipeline_v2.py` 中 FEniCSx 相关段落

---

## 推荐执行顺序（最小路径）
1. 任务 1 -> 2 -> 3 -> 4 -> 5
2. 任务 6 -> 7 -> 8
3. 任务 9 -> 10
