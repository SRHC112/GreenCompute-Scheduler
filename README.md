# 绿算调度 GreenCompute-Scheduler

[![CI](https://github.com/SRHC112/GreenCompute-Scheduler/actions/workflows/ci.yml/badge.svg)](https://github.com/SRHC112/GreenCompute-Scheduler/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](requirements.txt)

> 面向宁夏（中卫—银川）算力枢纽的**多智能体算力调度编排系统**
> 2026 第五届"未来杯"创新大赛 · 赛道一：Agent 编排创新应用
> 仓库：<https://github.com/SRHC112/GreenCompute-Scheduler> · 主分支：`main` · 许可：Apache-2.0

把「一批算力作业交给谁跑、什么时候跑、跑在哪个资源池」这件事，
从规则脚本升级为一个**七角色、八状态、可定向返工**的多智能体编排系统：
用领域知识包给出显式目标函数，用五层验证器给出可归因的缺陷，
用自省闭环把缺陷**只退给责任节点**，而不是整条链重跑。

**为什么容易复现**：零框架、零数据库、零 GPU、无需联网，除 PyYAML 外没有第三方运行时依赖；
全部指标由确定性模拟器生成，**同一种子必然得到同一组结果**，评审可现场复现。

---

## 一、环境要求

| 项目 | 要求 |
|---|---|
| Python | **3.10+**（已在 Python 3.13.14 实测通过） |
| 第三方依赖 | `PyYAML>=6.0,<7.0`，唯一的运行时依赖 |
| 操作系统 | Windows / macOS / Linux |
| 网络 | 不需要；仅「可选接入大模型」时需要 |
| 端口 | 本地 `8000`（可视化操作台，可改） |
| 硬件 | 普通笔记本即可，无需 GPU / 数据库 / Docker |

> 可视化操作台 `app/server.py` 只用标准库 `http.server`；看板 `console.html` 是单文件零依赖 HTML，
> 不引用任何 CDN，离线双击即可查看。

---

## 二、安装

### 2.1 一键安装（推荐）

只需 **Python 3.10+**；安装脚本本身只用标准库，运行前不需要装任何依赖。

**方式 A：已下载或 clone 了整个项目**

```bat
:: Windows：双击 install.bat，或在项目目录执行
install.bat
```

```bash
# macOS / Linux
bash install.sh
```

也可跨平台统一执行 `python install.py`。

**方式 B：只下载了 `install.py` 单个文件**

把脚本放到任意空目录后运行，它会自动 clone 源码到 `./GreenCompute-Scheduler` 并完成安装：

```bash
python install.py
python install.py --dir D:\dev\GreenCompute-Scheduler   # 自定义目标目录
```

**脚本自动完成的步骤**：预检（Python 版本 / 项目结构 / 写权限）→ 创建 `.venv` 并安装依赖
→ 跑 74 个单元测试（其中 test_smoke.py 12 个是冒烟测试）与一次**离线**编排演示（不消耗模型额度）→ 准备 `runs/` 与运行入口
→ 询问是否接入大模型 → 打印安装结果汇总。任何步骤失败都会给出中文原因与修复建议，退出码非 0。

**唯一的一次交互确认**

```
是否接入大模型（OpenAI 兼容接口，如 DeepSeek / GLM / Qwen）？
  接入：模型负责归因叙述与语义 Judge；不接入：使用离线确定性模式，
  全流程可跑通且指标数值不变。
  请选择 [y/N]：
```

| 选择 | 脚本行为 |
|---|---|
| **不接入**（直接回车 / `n`） | 跳过全部模型配置，**不创建 `.env`**，继续完成安装；系统按离线确定性模式运行，指标数值不变 |
| **接入**（`y`） | 依次询问接口地址、模型名、API Key（可回车用默认值，Key 输入不回显），写入 `.env` 并回读校验，确认项目进入 `online` 模式；**不实际调用模型接口**，不会消耗额度 |

> `.env` 由运行入口在启动时加载为环境变量——项目本身只读 `os.getenv`，不会自动读 `.env`，
> 因此**不需要改动任何现有源码**。已存在的 `.venv` / `.env` / `run.bat` / `run.sh` 一律不覆盖，脚本可重复运行。
> 自动化场景：`python install.py --no-llm`（离线）或 `--llm`（配合 `GCS_LLM_*` 环境变量）可完全免交互；
> `--check` 只做只读预检。

**安装完成后的快捷运行**（无需手动激活虚拟环境）

```bat
run.bat demo        :: 完整编排演示
run.bat server      :: 启动可视化操作台
run.bat test        :: 冒烟测试
run.bat eval        :: 对照实验
```

```bash
./run.sh demo       # macOS / Linux
python install.py --run -- python -m gcs.demo --policy fcfs   # 自定义命令
```

> 完整的命令、参数与产物说明见「三、运行方法」；手工安装方式与本文后续原有命令完全不受影响。

### 2.2 手动安装（3 步）

#### Windows（PowerShell / CMD）

```bat
git clone https://github.com/SRHC112/GreenCompute-Scheduler.git
cd GreenCompute-Scheduler

python -m venv .venv
.venv\Scripts\activate

python -m pip install -r requirements.txt
python -c "import yaml; print('PyYAML', yaml.__version__)"
```

#### macOS / Linux

```bash
git clone https://github.com/SRHC112/GreenCompute-Scheduler.git
cd GreenCompute-Scheduler

python3 -m venv .venv
source .venv/bin/activate

python -m pip install -r requirements.txt
python -c "import yaml; print('PyYAML', yaml.__version__)"
```

> **关键**：安装与运行请使用同一个解释器（`python -m pip install ...` 与 `python -m gcs.demo ...` 成对出现），
> 可避免「装了却 import 不到」这类最常见的坑。
> 若本机 `python` 不可用：Windows 用 `py -3 -m venv .venv`，macOS/Linux 用 `python3`。

---

## 三、运行方法

### 3.1 冒烟测试（先跑这个，确认环境 OK）

```bash
python -m unittest discover -s tests -v
```

预期输出：`Ran 65 tests ... OK`，耗时 < 1 秒（实测 0.042s）。

### 3.2 一键完整编排（核心演示）

```bash
python -m gcs.demo --jobs 60 --seed 20261111
```

终端打印 SLA 达成率、单位成本、绿电占比、碳排、token 等摘要，
并在 `runs/run_<时间戳>_full/` 下生成：

| 文件 | 内容 |
|---|---|
| `console.html` | 单文件零依赖可视化看板（DAG / 运行轨迹 / 指标），双击即可离线打开 |
| `report.md` | Markdown 版运行报告 |
| `artifact.json` | 完整编排产物（审计日志、缺陷归因、返工记录） |
| `metrics.json` | 结构化指标，便于脚本复算 |
| `runs/latest.html` | 始终指向最近一次看板 |

**可用参数**

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--jobs` | `40` | 作业数量 |
| `--seed` | `20261111` | 随机种子；相同种子 + 相同参数必然得到相同指标 |
| `--policy` | `full` | `full` / `fcfs` / `sjf` / `generic` / `no_reflect` / `no_green` |
| `--knowledge` | `knowledge/ningxia_cluster.yaml` | 领域知识包路径 |

例如跑对照组：`python -m gcs.demo --policy fcfs --jobs 60 --seed 7`

### 3.3 可视化操作台

```bash
python app/server.py --open
```

浏览器打开 <http://127.0.0.1:8000/>，包含四个区块：任务发起 / DAG 拓扑 / 运行轨迹 / 评测看板。
参数：`--host`（默认 `127.0.0.1`）、`--port`（默认 `8000`）、`--open`（启动后自动打开浏览器）。
按 `Ctrl+C` 停止服务。

### 3.4 对照实验

```bash
python evaluation/run_eval.py
```

按 **6 组配置 × 3 个随机种子、每组 80 个作业**运行，产出：

- `evaluation/results.csv` — 逐次原始结果（18 行，可追溯到每一次运行）
- `evaluation/summary.md` / `summary.json` — 均值 ± 标准差与相对提升
- `evaluation/eval_report.html` — 可直接截图的对比看板

> `runs/` 已被 `.gitignore` 忽略，运行产物不入库；`evaluation/` 下的评测结果作为作品证据随仓库提交。
> ⚠️ 重跑该脚本会**覆盖**上述已提交的评测文件，提交前请确认是否要保留原数据。

### 命令速查

| 目的 | 命令 |
|---|---|
| 环境自检 | `python -m unittest discover -s tests -v` |
| 完整演示 | `python -m gcs.demo --jobs 60 --seed 20261111` |
| 单组快速调参 | `python evaluation/probe.py` |
| 缺陷明细排查 | `python evaluation/debug_defects.py` |
| 对照实验 | `python evaluation/run_eval.py` |
| 可视化操作台 | `python app/server.py --open` |

---

## 四、验证清单（如何确认跑对了）

1. `python -m unittest discover -s tests -v` → **74 个单元测试全绿（其中 test_smoke.py 12 个是冒烟测试）**。
2. **确定性复现**：连续两次执行 `python -m gcs.demo --jobs 20 --seed 7`，对比两次 `runs/run_*/metrics.json`，核心指标应完全一致。
3. `runs/latest.html` 存在且浏览器可打开，页面不依赖外网。
4. `python evaluation/run_eval.py` 后 `evaluation/results.csv` 含 6 组 × 3 种子的数据行。

### 4.1 GitHub Actions 自动验证

推送到 `main` 或提交 PR 时，`.github/workflows/ci.yml` 会自动执行（也可在 Actions 页面手动触发）：

| Job | 覆盖内容 |
|---|---|
| `test` | Ubuntu × Python 3.10 / 3.11 / 3.12 / 3.13 + Windows × 3.13；语法编译、单元测试（全量），一键编排、看板产物校验、操作台 HTTP 探测、trace_fit 参数对齐校验 |
| `evaluation` | 六组对照实验，校验 `results.csv` 为 18 行且每组 3 次，检查三份报告文件 |

看板与评测报告会作为 Artifact 上传，分别保留 7 / 14 天，可在 Actions 运行页下载。
CI 使用的命令与本文第三、四节完全一致，因此**本地能跑通 ≈ CI 能跑通**；工作流细节与常见坑见 [docs/DEPLOY.md](docs/DEPLOY.md) 第十节。

---

## 五、可选：接入国产大模型

不配置任何环境变量也能跑通全流程：模型层退化为**离线确定性模板**，指标数值不变。
接上模型后，模型负责归因叙述与语义层 Judge。

**Windows**

```bat
set GCS_LLM_BASE_URL=https://api.deepseek.com/v1
set GCS_LLM_API_KEY=sk-xxxxxxxx
set GCS_LLM_MODEL=deepseek-chat
python -m gcs.demo --jobs 60
```

**macOS / Linux**

```bash
export GCS_LLM_BASE_URL=https://api.deepseek.com/v1
export GCS_LLM_API_KEY=sk-xxxxxxxx
export GCS_LLM_MODEL=deepseek-chat
python -m gcs.demo --jobs 60
```

| 变量 | 说明 |
|---|---|
| `GCS_LLM_BASE_URL` | OpenAI 兼容接口地址 |
| `GCS_LLM_API_KEY` | 接口密钥 |
| `GCS_LLM_MODEL` | 主模型名，默认 `deepseek-chat` |
| `GCS_LLM_LITE_MODEL` | 轻量模型名（可选，如 `qwen-turbo`）；留空时 light 层回落主模型 |

支持 DeepSeek、智谱 GLM、阿里 Qwen、火山 Doubao、百度千帆、MiniMax 等 OpenAI 兼容接口。

**模型路由与自定义适配器**（`gcs/llm.py`）：

- 两级路由：`chat(..., tier="primary")`（默认，归因叙述 / 语义 Judge）与 `tier="light"`（轻量任务）；
  配置 `GCS_LLM_LITE_MODEL` 后 light 层走轻量模型，未配置自动回落主模型；
- 自定义适配器：继承 `OpenAICompatAdapter` 覆盖 `complete()`，经 `ModelGateway(adapter=...)`
  注入即可接管全部在线请求（不同 API 方言 / 自建代理 / mock 测试），预算、降级与记账逻辑不受影响；
- `gateway.stats()` 额外返回 `model_lite` / `adapter` / `tier_stats`（分层模型与 token 用量）。

> **安全提示**：密钥只通过环境变量传入，**代码不会自动加载 `.env`**，绝不要写进源码或提交到仓库；
> `.env` / `.env.local` 已在 `.gitignore` 中排除。
> **设计纪律**：所有指标数字由确定性模块计算，模型只产出叙述性文本；
> 模型不可用时自动降级并继续运行，不影响评测得分数值。

---

## 六、目录结构

```
GreenCompute-Scheduler/
├── knowledge/
│   └── ningxia_cluster.yaml      领域知识包（电价/绿电/资源池/作业画像/约束阈值/返工预算）
├── gcs/                           编排内核
│   ├── knowledge.py               知识包加载、校验、指纹、查询接口
│   ├── simulator.py               算力模拟器（L0）：作业流生成、事件驱动执行、故障注入、成本核算
│   ├── dag.py                     DAG 构图、并行组（levels）、返工边
│   ├── agents.py                  七角色角色卡 + Profiler/Scheduler/Executor/Reporter
│   ├── verifier.py                五层验证器（L1 结构 / L2 数值 / L3 约束 / L4 可达性 / L5 语义）
│   ├── reflect.py                 六维 rubric、自省打分、归因与定向返工指令
│   ├── llm.py                     模型网关（国产 OpenAI 兼容接口 + 离线确定性降级 + token 记账）
│   ├── orchestrator.py            八状态机编排引擎、Policy 对照组开关、审计日志
│   ├── report.py                  单文件零依赖看板 console.html + Markdown 报告
│   └── demo.py                    一键演示 CLI
├── evaluation/
│   ├── run_eval.py                六组配置 × 三种子对照实验主脚本
│   ├── probe.py                   单种子快速对比（调参用）
│   ├── debug_defects.py           缺陷明细排查
│   ├── results.csv                逐次原始结果（随作品提交）
│   ├── summary.md / summary.json  均值 ± 标准差
│   └── eval_report.html           对比看板（可直接截图）
├── app/
│   └── server.py                  零依赖可视化操作台（仅标准库）
├── tests/
│   └── test_smoke.py              12 个冒烟测试（含 Pareto 权重缩放、trace_fit 参数对齐）；tests/ 合计 74 个
├── docs/
│   ├── 架构说明.md                 架构与关键设计说明
│   ├── DEPLOY.md                  Git 部署/推送流程与排障
│   ├── 参数出处.md                 每个关键参数的取值、来源与可信度表
│   ├── 性能与并发.md              实测吞吐、并发加速比与可复现性结论
│   └── THIRD_PARTY.md             第三方依赖与许可声明
├── runs/                          运行产物（.gitkeep 占位，内容不入仓）
├── .gitignore
├── requirements.txt
└── LICENSE                       Apache-2.0
```

---

## 七、常见问题（排障）

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: No module named 'yaml'` | 未装依赖，或装到了别的解释器 | `python -m pip install -r requirements.txt`，并用同一个 `python` 运行 |
| `python` 不是内部或外部命令 | Python 未加入 PATH | Windows 用 `py -3`；或重装时勾选 "Add to PATH" |
| Windows 控制台中文/货币符号乱码 | 终端编码非 UTF-8 | 程序已强制 UTF-8 输出；仍乱码时执行 `chcp 65001` |
| `OSError: [WinError 10048]` 端口被占用 | 8000 已被其他程序占用 | `python app/server.py --port 8080` |
| 找不到 `console.html` | 查看了旧的运行目录 | 以终端打印的「产物」路径为准，或直接打开 `runs/latest.html` |
| 模型调用报错 / 超时 | 网络或密钥问题 | 不配置也能跑，系统自动降级，指标数值不变 |
| `git push` 失败、提示代理连不上 | 本机 Git 代理配置失效 | 见 [docs/DEPLOY.md](docs/DEPLOY.md) 第六节 |

---

## 八、对照实验怎么读

| 组别 | 含义 | 变量控制 |
|---|---|---|
| G-A | FCFS 规则调度 | 无 Agent，无领域知识 |
| G-B | SJF + 约束优先 | 规则调度里做过一轮优化的天花板 |
| G-C | 通用 Agent 直接调度（**行为模拟**） | 统一权重、不做窗口对齐、单轮不返工 |
| G-D | **本方案** | 知识包 + 多目标调度 + 自省返工 |
| G-E1 | 消融：去掉自省闭环 | 只跑一轮，无验证—归因—返工 |
| G-E2 | 消融：去掉绿电权重 | 目标函数中绿电/碳排权重置零 |

六组共用**同一份模拟器数据、同一份知识包版本、同一份评测脚本**，
只有 Policy 开关不同——这保证对照实验站得住。
G-C 为行为模拟对照，答辩时须如实标注，而非真实第三方框架运行结果。

评测脚本除六组对照外，还输出两份反事实扫描（都在 `evaluation/summary.md` 里）：

- **参数敏感性**：返工阈值 60/70/80/90/95、绿电权重 0/15/35/60%，用于回答「结论是不是调参调出来的」；
- **排队—成本帕累托前沿**：只放大排队权重、约束与数据不变，回答「平均排队从 13.8 分钟降到 0.4 分钟，这个代价值不值、能不能换回来」。

结论的措辞纪律：置信区间跨 0 的指标一律只写“方向性”，不写成确定性收益。

## 九、核心设计（一句话版）

| 机制 | 做法 | 带来什么 |
|---|---|---|
| 领域知识包 | 电价/绿电/资源池/作业画像全部外置并版本化 | 代码里没有任何「经验常数」，可审计、可替换 |
| 显式目标函数 | J = w_wait·等待 + w_cost·成本 + w_green·绿电 + w_carbon·碳排 + w_rel·拥塞 | 权衡是**算出来的**不是拍脑袋的 |
| 硬约束优先 | 截止时间不满足的候选直接出局，无解才退化 | SLA 不与成本做加权折中 |
| 五层验证 | 结构/数值/约束/可达性/语义，能用规则判的不给模型判 | 缺陷可复算、可归因 |
| 定向返工 | 缺陷带 owner+node，只重跑责任子树 | 返工不是整链重跑，轮次与成本可控 |
| 失败即资产 | 实测超时比例写回画像经验库 | 系统越用越准 |

---

## 十、文档索引

| 文档 | 内容 |
|---|---|
| [README.md](README.md) | 项目说明、安装与运行方法（本文） |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Git 部署与推送流程、认证配置、代理排障 |
| [docs/架构说明.md](docs/架构说明.md) | 架构与关键设计说明 |
| [docs/参数出处.md](docs/参数出处.md) | 每个关键参数的取值、来源与可信度表 |
| [docs/性能与并发.md](docs/性能与并发.md) | 实测吞吐、并发加速比与可复现性结论 |
| [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md) | 第三方依赖与许可声明 |
| [docs/流程基线符合性记录.md](docs/流程基线符合性记录.md) | 《产品项目研发流程规范》31 项产出物逐项事实对照 |
| [docs/差异及处理方式记录.md](docs/差异及处理方式记录.md) | 流程口径差异 D-01~D-10、内容规格偏离 S-01~S-08 登记与审批占位 |
| [docs/分支与合并规范.md](docs/分支与合并规范.md) | 五类分支、合并路径、Code Review 规则与现状过渡方案 |
| [docs/闭环确认清单.md](docs/闭环确认清单.md) | H/M/L 问题逐项闭环状态与证据来源 |
| [docs/竞赛要求_2026未来杯_官方原文.md](docs/竞赛要求_2026未来杯_官方原文.md) | 2026 第五届未来杯赛题一官方要求原文（GCS-REQ-CONTEST-2026-001） |
| [docs/竞赛符合性对照_2026未来杯.md](docs/竞赛符合性对照_2026未来杯.md) | 官方要求逐条对照：硬门槛、初赛必交件、决赛补充与缺口优先级（GCS-CONF-CONTEST-2026-001） |
| [docs/SRS_需求规格说明书.md](docs/SRS_需求规格说明书.md) | 自建需求规格说明书 |
| [docs/RTM_需求追踪矩阵.md](docs/RTM_需求追踪矩阵.md) | 需求追踪矩阵 |
| [docs/测试计划.md](docs/测试计划.md) / [docs/测试用例.md](docs/测试用例.md) / [docs/测试报告.md](docs/测试报告.md) | 测试三件套 |
| [docs/验收单.md](docs/验收单.md) / [docs/回滚预案.md](docs/回滚预案.md) / [docs/复盘记录.md](docs/复盘记录.md) | 验收与复盘 |

## 十一、许可与引用

本仓库基于 **Apache-2.0** 许可（见 `LICENSE`），第三方依赖与许可见 `docs/THIRD_PARTY.md`。
设计参考了 AgentScope 2.0（Apache-2.0）的角色化与消息传递思想，
以及长程 Agent Harness 类项目的「计划—执行—观察—重规划」范式，
但编排引擎、DAG、验证器、rubric 与返工机制均为本项目自研实现。
