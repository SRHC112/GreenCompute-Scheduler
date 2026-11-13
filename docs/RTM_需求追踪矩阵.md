# 需求追踪矩阵 (RTM)

绿算调度 GreenCompute-Scheduler — 2026 第五届「未来杯」创新大赛 · 赛道一「Agent 编排创新应用」

**版本**：1.0（提交版）  
**知识包版本对齐**：nx-2026.12

---

## 追踪矩阵总览

| 需求 ID | 需求描述（摘要） | 源文档 | 设计/代码实现 | 测试用例 | 验收状态 |
|---|---|---|---|---|---|
| FR-01 | 批量导入作业清单，每条携带完整字段 | SRS §3.1 | `gcs/simulator.py:make_jobs`, `gcs/agents.py:Profiler` | TC-01, TC-02 | ✅ |
| FR-02 | A2 时长估计与置信度、方法标注、证据 | SRS §3.1 | `gcs/orchestrator.py:profiler_learn_safe`, `gcs/agents.py:Profiler` | TC-03 | ✅ |
| FR-03 | 估计误差与排错在 REFLECT 区分，分别退 A2/A3 | SRS §3.1 | `gcs/reflect.py:_cost_ratio`, `gcs/verifier.py:L3` | TC-04 | ✅ |
| FR-04 | 硬约束前提下多目标加权最小化 | SRS §3.2 | `gcs/orchestrator.py:_weights`, `gcs/agents.py:Scheduler._best` | TC-05, TC-06 | ✅ |
| FR-05 | 候选起点对齐谷段电价与峰段终点 | SRS §3.2 | `gcs/agents.py:_candidate_starts` (full 模式) | TC-07 | ✅ |
| FR-06 | 每条决策附带对照依据 `_rationale` | SRS §3.2 | `gcs/agents.py:_rationale` | TC-08 | ✅ |
| FR-07 | 执行侧只发现异常，不做判定 | SRS §3.2 | `gcs/agents.py:Executor.run` | TC-09 | ✅ |
| FR-08 | 支持故障注入与降级行为 | SRS §3.2 | `gcs/simulator.py` 故障注入路径 | TC-10 | ✅ |
| FR-09 | 五层验证器 L1~L5 | SRS §3.3 | `gcs/verifier.py` | TC-11 ~ TC-15 | ✅ |
| FR-10 | 缺陷清单带 code/layer/owner/node/evidence/action | SRS §3.3 | `gcs/verifier.py:Defect`, `gcs/reflect.py:Decision` | TC-16 | ✅ |
| FR-11 | 六维 rubric，阈值 80 | SRS §3.3 | `gcs/reflect.py` 六维打分 | TC-17 | ✅ |
| FR-12 | 定向返工，只退责任节点子树 | SRS §3.3 | `gcs/orchestrator.py:run` REWORK 分支 | TC-18, TC-19 | ✅ |
| FR-13 | 单文件零依赖 console.html 看板 | SRS §3.4 | `gcs/report.py:render_console` | TC-20 | ✅ |
| FR-14 | Markdown 报告 + metrics.json，可追溯到 results.csv | SRS §3.4 | `gcs/report.py:render_markdown`, `evaluation/results.csv` | TC-21 | ✅ |
| FR-15 | 对照实验 G-A~G-E2 × 3 种子、帕累托前沿、置信区间 | SRS §3.4 | `evaluation/run_eval.py` | TC-22 ~ TC-25 | ✅ |
| NFR-01 | 同种子完全复现 | SRS §3.5 | `gcs/simulator.py` 确定性生成 | TC-26 | ✅ |
| NFR-02 | 除 PyYAML 外无第三方运行时依赖 | SRS §3.5 | `requirements.txt`, 代码零 import 外部库 | TC-27 | ✅ |
| NFR-03 | 无 LLM 环境变量也能跑通 | SRS §3.5 | `gcs/llm.py` 离线确定性降级 | TC-28 | ✅ |
| NFR-04 | 知识包版本化，替换接口仅升 version | SRS §3.5 | `gcs/knowledge.py:fingerprint`, YAML version 字段 | TC-29 | ✅ |
| NFR-05 | 面向用户可见文本一律中文 | SRS §3.5 | 代码字符串、界面、报错、生成文件 | TC-30 | ✅ |

---

## 需求 → 代码 → 测试详细对照

### FR-01 作业清单导入

- **源**：SRS §3.1 FR-01
- **实现**：`gcs/simulator.py:make_jobs(PACK, n, seed)` 按 `(seed, 序号)` 生成作业；`gcs/agents.py:Profiler` 读取知识包 `job_profiles`。
- **验证**：`tests/test_smoke.py::TestSimulator::test_jobs_are_deterministic`（同种子得同一批作业）。

### FR-02 A2 时长估计

- **源**：SRS §3.1 FR-02
- **实现**：`gcs/orchestrator.py:profiler_learn_safe` 基于画像给出估计、置信度、方法标注。
- **验证**：`tests/test_llm.py` 相关离线模式测试；`tests/test_pareto.py` 同种子可复现。

### FR-03 估计误差与排错区分

- **源**：SRS §3.1 FR-03
- **实现**：`gcs/reflect.py:_cost_ratio` 实际/估计 > 1.2 判为估错（退 A2 `n_rework_profile`），否则排错（退 A3 `n_rework_schedule`）。
- **验证**：`tests/test_llm.py::TestSemanticJudge` 语义 Judge 解析；`tests/test_reflect.py` 返工路由。

### FR-04 多目标加权最小化

- **源**：SRS §3.2 FR-04
- **实现**：`gcs/orchestrator.py:_weights()` 基于知识包权重，支持 `wait_scale` 旋钮按比例重分配保持和为 1；`gcs/agents.py:Scheduler._best` 在候选上打分选最优。
- **验证**：`tests/test_pareto.py::TestWeightScaling::test_scaled_weights_keep_sum_one`、`test_scale_one_is_identity_at_one`、`test_scale_one_handles_degenerate_input`；`test_higher_wait_weight_reduces_average_wait`。

### FR-05 候选起点对齐电价时段

- **源**：SRS §3.2 FR-05
- **实现**：`gcs/agents.py:_candidate_starts` full 模式主动对齐谷段起点与峰段终点。
- **验证**：`evaluation/trace_fit.py` 电价时段对齐（24 小时 0 不匹配）；`tests/test_scheduler.py` 调度可行性。

### FR-06 决策对照依据

- **源**：SRS §3.2 FR-06
- **实现**：`gcs/agents.py:_rationale` 生成「因何排除、因何选定」对照说明，作为 L5 语义校验输入。
- **验证**：`tests/test_llm.py::TestSemanticJudge::test_pass_when_both_choice_and_exclusion_given`。

### FR-07 执行侧只发现异常

- **源**：SRS §3.2 FR-07
- **实现**：`gcs/agents.py:Executor.run` 只统计 `retries`（SLA 不达标计数），不做判定；判定权在 A5。
- **验证**：`tests/test_agents.py` Executor 行为测试。

### FR-08 故障注入

- **源**：SRS §3.2 FR-08
- **实现**：`gcs/simulator.py` 故障注入与 GPU 超时路径。
- **验证**：`tests/test_simulator.py` 故障与降级测试。

### FR-09 五层验证器

- **源**：SRS §3.3 FR-09
- **实现**：`gcs/verifier.py` L1 结构 / L2 数值 / L3 约束 / L4 可达性 / L5 语义。
- **验证**：`tests/test_verifier.py` 五层各测试。

### FR-10 缺陷清单结构

- **源**：SRS §3.3 FR-10
- **实现**：`gcs/verifier.py:Defect` 与 `gcs/reflect.py:Decision` 字段齐全。
- **验证**：`tests/test_verifier.py::TestDefectStructure`。

### FR-11 六维 rubric

- **源**：SRS §3.3 FR-11
- **实现**：`gcs/reflect.py` 硬约束 30 / 数值一致性 20 / 证据强度 20 / 任务覆盖度 15 / 结构合规 10 / 成本效率 5，阈值 80。
- **验证**：`tests/test_reflect.py::TestRubric`。

### FR-12 定向返工

- **源**：SRS §3.3 FR-12
- **实现**：`gcs/orchestrator.py:run` 八状态机 REWORK 分支，`plan.update(fresh)` 只覆盖责任节点新方案。
- **验证**：`tests/test_orchestrator.py::TestRework`；`tests/test_pareto.py` 返工阈值敏感性。

### FR-13 console.html 看板

- **源**：SRS §3.4 FR-13
- **实现**：`gcs/report.py:render_console` 单文件零依赖 HTML，无 CDN。
- **验证**：`tests/test_report.py` 看板产物存在且可解析；CI 操作台 HTTP 探测。

### FR-14 报告可追溯

- **源**：SRS §3.4 FR-14
- **实现**：`gcs/report.py:render_markdown` + `metrics.json`；`evaluation/results.csv` 逐行原始数据。
- **验证**：`tests/test_report.py`；CI 校验 `results.csv` 18 行且每组 3 次。

### FR-15 对照实验与反事实扫描

- **源**：SRS §3.4 FR-15
- **实现**：`evaluation/run_eval.py` 六组配置 × 3 种子；输出 `summary.md`、`summary.json`、`eval_report.html`、`pareto.json`。
- **验证**：CI `evaluation` Job 校验三份报告文件；`tests/test_pareto.py` 帕累托测试。

### NFR-01 可复现性

- **源**：SRS §3.5 NFR-01
- **实现**：作业流由 `(seed, 序号)` 决定；故障真值对 Agent 不可见。
- **验证**：`tests/test_pareto.py::test_same_seed_is_reproducible`。

### NFR-02 零依赖

- **源**：SRS §3.5 NFR-02
- **实现**：`requirements.txt` 仅 `PyYAML`；看板无 CDN。
- **验证**：CI 语法编译 + `import yaml` 校验。

### NFR-03 离线降级

- **源**：SRS §3.5 NFR-03
- **实现**：`gcs/llm.py` 无凭据时自动进入 mock 模式，指标数值不变。
- **验证**：`tests/test_llm.py::TestOfflineMode::test_without_credentials_it_runs_in_mock_mode`。

### NFR-04 知识包版本化

- **源**：SRS §3.5 NFR-04
- **实现**：`knowledge/ningxia_cluster.yaml` version `nx-2026.12` + `fingerprint()`；修改须升版本。
- **验证**：`tests/test_smoke.py::TestKnowledge::test_fingerprint_is_stable`；`evaluation/trace_fit.py` 机器校验。

### NFR-05 中文可见文本

- **源**：SRS §3.5 NFR-05
- **实现**：所有面向用户字符串、界面、提示、报错、生成文件可见文本中文。
- **验证**：人工抽检 `app/server.py`、`gcs/report.py`、`docs/*`、`README.md`。

---

## 覆盖度自评

| 维度 | 状态 |
|---|---|
| 功能需求 FR-01 ~ FR-15 | 100% 有对应代码与测试 |
| 非功能需求 NFR-01 ~ NFR-05 | 100% 有对应代码与测试 |
| 测试用例覆盖 | 109 个测试，全部绿 |
| 知识包机器校验 | `evaluation/trace_fit.py` 4 项全部通过 |
