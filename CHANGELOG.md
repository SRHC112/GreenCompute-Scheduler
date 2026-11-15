# 变更日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格。

## [Unreleased] - 2026-11-15

当前版本：1.0.0（未发布），变更日期 2026-11-15。

### Added
- `gcs/knowledge.py`：知识包 `schema_version` 字段（整数，缺省视为 1），校验错误开始区分「版本过低，请补齐以下键」与「配置值非法」。
- `tests/test_knowledge_regression_20261115.py`：新增知识包回归测试（布尔值拒绝、旧包缺键诊断、权威包回归）。
- `CHANGELOG.md`：本项目变更日志。
- `install.sh`：Python 按版本择优选择（`python3.13`…`python3.10`、`python3`、`python`），要求 >= 3.10，均不满足时给出中文提示与安装指引。

### Fixed
- 知识包：旧包（无 `schema_version` 或声明 1）缺 `constraints.max_budget_overrun` / `rework_budget.max_tokens` / `max_seconds` 时，给出「缺少」+ 具体键名 + 所在段 + 应在文件哪段补齐的中文提示，失败友好且可诊断（不再裸 KeyError）。
- 知识包：`pass_score: true` / `max_rounds: true` 在数值读取/转换阶段被拒绝，报中文错误「必须是数值，不能是布尔值」；`validate()` 既有 bool 守卫转为真实第二道防线。
- 知识包：`knowledge/ningxia_cluster.yaml` 缺 `schema_version` 时按 v1 处理，键集齐全（含 v2 新增键）不报错。
- 知识包：`constraints` / `rework_budget` 缺整个段或缺字段时统一给出可操作中文错误。
- `install.sh`：shebang 固定 `#!/usr/bin/env bash`；强制 LF 换行（通过 `newline=""` 写入）；没带 CR。
- `install.py`：错误提示中指向不存在的命令 `python -m gcs.demo --check` 改为真实命令 `python -m gcs.demo --jobs 5`（冒烟演示）。
- `install.py`：`--check` 「必须在项目目录内运行」分支改为可达（按 `CWD` 判定），行为与提示一致。
- `install.py`：`setup_console()` / `_child_encoding()` 自洽统一为「脚本按 UTF-8 + errors="replace"，子进程按本地首选编码 + errors="replace"」，不再做 `chcp 65001` 变更；`install.py --check` 在中文 Windows 下输出可读。
- `install.py`：写入文本文件（.env 临时文件、探针、.gitkeep）统一 `newline="\n"`，避免 Windows CRLF 与 `.gitattributes` eol=lf 漂移。
- `gcs/metrics_meta.py`：`tokens` 表头/定义修正为「在线模型调用量；离线降级也累计、不置 0」，与 `ModelGateway` 离线降级仍累计的实现一致；其余指标核对无异。

### Changed
- `install.sh`：按版本择优选 Python，低于 3.10 的 `python3` 不再被直接选中（macOS 自带 3.9.6 场景下会报错并给出 `brew install python@3.12` / python.org 指引）。

### 本轮（2026-11-15 全面验证批次）修复明细

**评测与 CLI 入口**
- `evaluation/run_eval.py`：`--jobs` 增加上界（1-300，与 `gcs/demo.py` / `app/server.py` 统一），越界以 rc=2 + 中文提示退出，不再无界运行。
- `evaluation/run_eval.py`：`summary.json` / `summary.md` / `eval_report.html` 中的作业规模改为**本次实际值**（原写死 80），消除产物自相矛盾。
- `evaluation/run_eval.py`：`--outdir` 不可写/非目录、知识包非法均收敛为中文提示 + rc=2，不再泄漏裸栈。
- `evaluation/run_eval.py`：知识包加载改走 `gcs.knowledge.load_knowledge`，加载失败统一收敛为 `KnowledgeError`。
- `evaluation/run_eval.py`：修正 `body` 变量由 `list[str]` 复用为 `str` 的类型混淆（mypy `assignment`/`operator`）。
- `evaluation/trace_fit.py`：知识包缺失/非法改为中文提示 + rc=2，不再输出裸 traceback。
- `gcs/demo.py`：产物写盘失败收敛为中文提示 + 非零退出码，不再裸栈。
- `gcs/llm.py` / `gcs/verifier.py`：新增 `rubric_source`（`llm` / `offline-mock`），使**离线降级判定与在线模型判定可区分**，不再静默改变 rubric 口径。

**鉴权与 HTTP 服务端**
- `app/auth.py`：`load_env_file` 捕获集合补 `UnicodeError`，非 UTF-8 的 `.env` 不再让服务启动即崩（与 `install.py` 的同名逻辑对齐）；`AuthConfig` 增加可操作的中文 warning。
- `app/auth_tool.py`：`.env` 读写/权限处理与 `install.py` 对齐（含 `UnicodeError`）；`ModuleSpec` 可空性显式校验；删除未使用的 `subprocess` 导入。
- `app/server.py`：新增 `MIN_JOBS` / `MAX_JOBS` 常量（与 `gcs/demo.py` 同源），消除硬编码 300。
- `app/server.py`：`render_page()` 由 `.replace()` 链改为 `re.sub` **单遍替换**，插入值不再被二次扫描（消除二次替换注入）。
- `app/server.py`：KPI 副标题改为引用 `gcs/metrics_meta.py` 的口径单一来源，消除重复文案。
- `app/server.py`：文本写入统一 `newline="\n"`，Windows 与 Linux 产物字节一致。

**质量门禁**
- `mypy.ini`：改为纯 ASCII（`configparser` 按平台 locale 解码，英文 Windows runner 的 cp1252 会抛 `UnicodeDecodeError`）。
- CI：`concurrency` 保持 **workflow 级**（job 级 + matrix 共用 group 会导致 5 个矩阵组合互相取消）。
- CI：`requirements-ci.txt` 用环境标记区分 coverage 版本（`>=3.11` 用 7.17.3，`<3.11` 用 7.10.4，后者要求 Python < 3.11）。
- CI：5 个 CLI 入口（`run_eval.py` / `gcs/demo.py` / `app/server.py` / `app/auth_tool.py` / `trace_fit.py`）统一同时重配 **stdout 与 stderr**（argparse 的报错走 stderr，原先只配 stdout 会把中文变成 `\uXXXX` 转义）。
- CI：mypy 覆盖范围由 `gcs` 扩展到 `gcs app evaluation install.py`（20 个源文件），原先只查 `gcs/` 漏掉 `app/`、`evaluation/` 的 8 处类型缺陷。
- CI：4 个 `shell: python` 内联块统一加 UTF-8 编码头（cp1252 控制台下 `print` 中文会崩并掩盖真实失败原因）。
- `tests/test_install.py`：`icacls` 分支断言加 `os.name == "nt"` 平台守卫，并补齐 Unix `chmod 600` 分支的真实覆盖。
- `tests/test_eval.py`：子进程输出改显式 `encoding="utf-8"`，不再按 runner locale 解码。

**交付与打包**
- `.gitattributes` 补全（`*.ini` / `*.html` / `*.example` / `LICENSE`），`*.csv` 设 `-text` 以保留 RFC 4180 的 CRLF 行尾。
- `install.bat` 改为 **CRLF + 纯 ASCII**、含 `exit /b %GCS_RC%` 退出码透传、移除 `pause`（避免 CI 挂起）。
- 两份交付 zip（`参赛提交_2026未来杯.zip` / `GreenCompute-Scheduler_源码_aaa知足常乐.zip`）按当前源重建，逐文件 SHA256 与源一致，`install.sh` 保留 0755 执行位。

## [1.0.0] - 2026-10-25

- 初始提交：GreenCompute-Scheduler 2026 未来杯参赛作品。
